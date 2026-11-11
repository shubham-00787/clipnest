"""Endpoint tests that drive the ASGI app directly (no httpx needed) with the
yt-dlp calls stubbed out."""
import asyncio
import json
import socket

import pytest

from app import core, main


async def call(method, path, body=None, query="", headers=None):
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "path": path, "raw_path": path.encode(), "query_string": query.encode(), "headers": hdrs,
        "scheme": "http", "server": ("test", 80), "client": ("test", 1),
    }
    sent_body = False

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body or b"", "more_body": False}
        await asyncio.sleep(3600)

    out = {"status": None, "headers": {}, "body": b""}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode(): v.decode() for k, v in msg["headers"]}
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    await asyncio.wait_for(main.app(scope, receive, send), timeout=10)
    return out


def post_json(path, payload):
    return call("POST", path, json.dumps(payload).encode(), headers={"content-type": "application/json"})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "family")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("142.250.80.46", 0))])
    monkeypatch.setattr(main.ytdl, "get_info", lambda url, quality="medium": {
        "title": "Funny cat", "duration": 60, "thumbnail": None, "uploader": "someone", "site": "Youtube"})


def run(coro):
    return asyncio.run(coro)


def test_health():
    out = run(call("GET", "/api/health"))
    assert out["status"] == 200
    assert json.loads(out["body"]) == {"ok": True, "configured": True, "locked": True, "max_minutes": 15, "max_mb": 200}


def test_index_served():
    out = run(call("GET", "/"))
    assert out["status"] == 200 and b"vidfetch" in out["body"]


def test_prepare_wrong_key(monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    out = run(post_json("/api/prepare", {"url": "https://youtu.be/x", "key": "nope"}))
    assert out["status"] == 401
    assert json.loads(out["body"])["error"] == "This page needs its private link. Open the link you were given."


def test_prepare_ok_defaults_to_medium():
    out = run(post_json("/api/prepare", {"url": "check https://youtu.be/abc please", "key": "family"}))
    data = json.loads(out["body"])
    assert out["status"] == 200
    assert data["quality"] == "medium" and data["title"] == "Funny cat"
    token = data["download_url"].split("t=", 1)[1]
    payload = core.verify_token(token)
    assert payload["u"] == "https://youtu.be/abc" and payload["q"] == "medium"


def test_prepare_rejects_private_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("127.0.0.1", 0))])
    out = run(post_json("/api/prepare", {"url": "http://localhost/x", "key": "family"}))
    assert out["status"] == 400


def test_prepare_rejects_long_video(monkeypatch):
    monkeypatch.setattr(main.ytdl, "get_info", lambda url, quality="medium": {"title": "t", "duration": 7200})
    out = run(post_json("/api/prepare", {"url": "https://youtu.be/x", "key": "family"}))
    assert out["status"] == 413


def test_prepare_bad_body():
    out = run(call("POST", "/api/prepare", b"not json"))
    assert out["status"] == 400


def test_prepare_unconfigured_server(monkeypatch):
    monkeypatch.delenv("ACCESS_KEY")
    out = run(post_json("/api/prepare", {"url": "https://youtu.be/x", "key": ""}))
    assert out["status"] == 503


def test_file_streams_and_cleans_up(monkeypatch, tmp_path):
    seen = {}

    def fake_download(url, quality, workdir):
        seen["workdir"] = workdir
        path = tmp_path / "video.mp4"
        path.write_bytes(b"FAKE-MP4-DATA")
        # put it inside workdir so cleanup is real
        import shutil, pathlib
        dest = pathlib.Path(workdir) / "video.mp4"
        shutil.copy(path, dest)
        return dest

    monkeypatch.setattr(main.ytdl, "download", fake_download)
    token = core.sign_token({"u": "https://youtu.be/x", "q": "low", "n": "Funny: cat"})
    out = run(call("GET", "/api/file", query=f"t={token}"))
    assert out["status"] == 200
    assert out["body"] == b"FAKE-MP4-DATA"
    assert out["headers"]["content-type"].startswith("video/mp4")
    assert "Funny%20cat.mp4" in out["headers"]["content-disposition"]
    import os
    assert not os.path.exists(seen["workdir"])  # temp folder deleted after sending
    assert main._active == 0


def test_file_rejects_bad_token():
    out = run(call("GET", "/api/file", query="t=bogus"))
    assert out["status"] == 400


def test_file_error_is_html_for_browsers(monkeypatch):
    def boom(url, quality, workdir):
        raise core.UserError("That post is <private>.", 422)
    monkeypatch.setattr(main.ytdl, "download", boom)
    token = core.sign_token({"u": "https://youtu.be/x", "q": "low"})
    out = run(call("GET", "/api/file", query=f"t={token}", headers={"accept": "text/html,*/*"}))
    assert out["status"] == 422
    assert b"&lt;private&gt;" in out["body"] and b"<private>" not in out["body"]
    assert main._active == 0


def test_file_busy(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT", "1")
    monkeypatch.setattr(main, "_active", 1)
    token = core.sign_token({"u": "https://youtu.be/x", "q": "low"})
    out = run(call("GET", "/api/file", query=f"t={token}"))
    assert out["status"] == 429


def test_prepare_rejects_known_oversize(monkeypatch):
    monkeypatch.setattr(main.ytdl, "get_info", lambda url, quality="medium": {"title": "t", "duration": 60, "size_bytes": 900 * 1024 * 1024})
    out = run(post_json("/api/prepare", {"url": "https://youtu.be/x", "key": "family", "quality": "best"}))
    assert out["status"] == 413
    assert "lower quality" in json.loads(out["body"])["error"]


def test_prepare_passes_quality_and_size_through(monkeypatch):
    seen = {}
    def fake(url, quality="medium"):
        seen["quality"] = quality
        return {"title": "t", "duration": 60, "size_bytes": 12 * 1024 * 1024}
    monkeypatch.setattr(main.ytdl, "get_info", fake)
    out = run(post_json("/api/prepare", {"url": "https://youtu.be/x", "key": "family", "quality": "low"}))
    data = json.loads(out["body"])
    assert seen["quality"] == "low" and data["size_bytes"] == 12 * 1024 * 1024



def test_app_starts_without_public_folder(monkeypatch):
    """On Vercel, public/ is served by the CDN and isn't inside the Python function."""
    import importlib
    import pathlib

    folder = main.PUBLIC_DIR
    real_is_dir = pathlib.Path.is_dir
    monkeypatch.setattr(pathlib.Path, "is_dir", lambda self: False if self == folder else real_is_dir(self))
    try:
        importlib.reload(main)                      # used to crash: "Directory ... does not exist"
        assert run(call("GET", "/api/health"))["status"] == 200
    finally:
        monkeypatch.undo()
        importlib.reload(main)
