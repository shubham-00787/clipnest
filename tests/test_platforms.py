"""Per-platform checks with realistic links and the messy text people actually
paste (share messages, tracking parameters, emoji, other languages).

These run offline: they prove the app accepts, cleans and routes each kind of
link correctly. Whether a platform actually hands over its video is a live
question that only scripts/smoke.py can answer.
"""
import asyncio
import json
import socket

import pytest

from app import core, main, ytdl
from tests.test_api import post_json

# (platform, link as a person would copy it)
LINKS = [
    ("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("youtube", "https://youtu.be/dQw4w9WgXcQ?si=AbC123xyz"),
    ("youtube", "https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=42s"),
    ("youtube-shorts", "https://www.youtube.com/shorts/aqz-KE-bpKQ"),
    ("instagram", "https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=MWxyZHVtYg=="),
    ("instagram", "https://www.instagram.com/p/C1a2B3c4D5e/"),
    ("facebook", "https://www.facebook.com/watch/?v=1234567890123456"),
    ("facebook", "https://fb.watch/abcDEF123x/"),
    ("facebook-reel", "https://www.facebook.com/reel/1234567890123456"),
    ("facebook-share", "https://www.facebook.com/share/v/1AbCdEfGhI/"),
    ("snapchat", "https://www.snapchat.com/spotlight/W7_EDlXWTBiXAEEniNoMPwAAYaWtscXRoamhxAWRzXiLPAWRzXiGEAAAAAA"),
    ("tiktok", "https://www.tiktok.com/@someone/video/7123456789012345678"),
    ("tiktok-short", "https://vm.tiktok.com/ZMabc123/"),
    ("x", "https://x.com/someone/status/1234567890123456789"),
    ("reddit", "https://www.reddit.com/r/videos/comments/abc123/a_title/"),
]
IDS = [f"{p}-{i}" for i, (p, _) in enumerate(LINKS)]


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "family")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("142.250.80.46", 0))])


# ---- the link survives however it was shared ---------------------------------
SHARE_WRAPPERS = [
    "{u}",
    "  {u}  ",
    "Look at this 😂 {u}",
    "{u}\nvia the app",
    "देखो यह वीडियो {u} बहुत मज़ेदार है",
    "Funny video title\n{u}",
    "(see {u}).",
    "{u}, wow!",
]


@pytest.mark.parametrize("platform,url", LINKS, ids=IDS)
@pytest.mark.parametrize("wrapper", SHARE_WRAPPERS)
def test_link_is_found_in_any_share_message(platform, url, wrapper):
    assert core.extract_url(wrapper.format(u=url)) == url


@pytest.mark.parametrize("platform,url", LINKS, ids=IDS)
def test_link_passes_validation(platform, url):
    assert core.validate_url(url) == url


# ---- routing: only YouTube uses the proxy ------------------------------------
@pytest.mark.parametrize("platform,url", LINKS, ids=IDS)
def test_proxy_routing(platform, url, monkeypatch):
    monkeypatch.setenv("YTDLP_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.delenv("PROXY_ALL", raising=False)
    uses_proxy = "proxy" in ytdl.base_opts(url)
    assert uses_proxy == platform.startswith("youtube")


# ---- whole request path with the downloader stubbed --------------------------
@pytest.mark.parametrize("platform,url", LINKS, ids=IDS)
def test_prepare_accepts_each_platform(platform, url, monkeypatch):
    seen = {}

    def fake_info(u, quality="medium"):
        seen["url"], seen["quality"] = u, quality
        return {"title": f"{platform} video", "duration": 30, "site": platform}

    monkeypatch.setattr(main.ytdl, "get_info", fake_info)
    out = asyncio.run(post_json("/api/prepare", {"url": f"Check this out {url} ok", "key": "family"}))
    assert out["status"] == 200, out["body"]
    assert seen == {"url": url, "quality": "medium"}  # exact link, default quality
    token = json.loads(out["body"])["download_url"].split("t=", 1)[1]
    assert core.verify_token(token)["u"] == url


# ---- things that must be refused ---------------------------------------------
@pytest.mark.parametrize("bad", [
    "http://localhost:8000/secret",
    "http://169.254.169.254/latest/meta-data/",
    "http://192.168.1.1/video.mp4",
    "file:///etc/passwd",
    "ftp://example.com/video.mp4",
    "https://example.com:8443/video.mp4",
])
def test_unsafe_links_are_refused(bad, monkeypatch):
    # use real address checks for these (no fake DNS)
    monkeypatch.undo()
    monkeypatch.setenv("ACCESS_KEY", "family")
    with pytest.raises(core.UserError):
        core.validate_url(core.extract_url(bad))
