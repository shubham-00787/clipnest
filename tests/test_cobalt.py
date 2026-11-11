"""YouTube backup through community cobalt servers (network calls faked)."""
import io
import json

import pytest

from app import cobalt, core, ytdl
from tests.test_ytdl_run import FakeDownloadError, install_fake_ytdlp

YT = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
BOT = "ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you're not a bot. Use --cookies-from-browser"


class FakeResp(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def default_servers(monkeypatch):
    monkeypatch.delenv("COBALT_INSTANCES", raising=False)


# ---- settings ------------------------------------------------------------------
def test_instances_setting(monkeypatch):
    assert cobalt.instances() == list(cobalt.DEFAULT_INSTANCES)
    monkeypatch.setenv("COBALT_INSTANCES", "https://a.example/, https://b.example")
    assert cobalt.instances() == ["https://a.example", "https://b.example"]
    monkeypatch.setenv("COBALT_INSTANCES", "off")
    assert cobalt.instances() == [] and not cobalt.enabled()


def test_official_servers_are_not_used():
    assert not any("cobalt.tools" in i or "imput.net" in i for i in cobalt.DEFAULT_INSTANCES)


@pytest.mark.parametrize("quality,expected", [("low", "360"), ("medium", "480"), ("high", "720"), ("best", "max")])
def test_request_body_video(quality, expected):
    body = cobalt.request_body(YT, quality)
    assert body["videoQuality"] == expected and body["url"] == YT
    assert body["youtubeVideoCodec"] == "h264" and body["youtubeVideoContainer"] == "mp4"   # plays on every phone
    assert body["localProcessing"] == "disabled"


def test_request_body_audio():
    body = cobalt.request_body(YT, "audio")
    assert body["downloadMode"] == "audio" and "videoQuality" not in body


# ---- asking one server -----------------------------------------------------------
@pytest.mark.parametrize("answer,expected", [
    ({"status": "tunnel", "url": "https://s/tunnel?id=1", "filename": "a.mp4"}, ("https://s/tunnel?id=1", "a.mp4")),
    ({"status": "redirect", "url": "https://cdn/x.mp4"}, ("https://cdn/x.mp4", "")),
    ({"status": "picker", "picker": [{"type": "photo", "url": "https://p"}, {"type": "video", "url": "https://v"}]}, ("https://v", "")),
])
def test_ask_understands_answers(monkeypatch, answer, expected):
    monkeypatch.setattr(cobalt, "_post", lambda inst, body: answer)
    assert cobalt.ask("https://s", YT, "medium") == expected


def test_ask_reports_error_code(monkeypatch):
    monkeypatch.setattr(cobalt, "_post", lambda inst, body: {"status": "error", "error": {"code": "error.api.auth.jwt.missing"}})
    with pytest.raises(cobalt.CobaltError, match="jwt.missing"):
        cobalt.ask("https://s", YT, "medium")


# ---- downloading through the pool --------------------------------------------------
def test_download_falls_through_to_a_working_server(monkeypatch, tmp_path):
    answers = {
        cobalt.DEFAULT_INSTANCES[0]: cobalt.CobaltError("timed out"),
        cobalt.DEFAULT_INSTANCES[1]: {"status": "error", "error": {"code": "error.api.youtube.login"}},
        cobalt.DEFAULT_INSTANCES[2]: {"status": "tunnel", "url": "https://s3/tunnel?id=9", "filename": "youtube_x_480p.mp4"},
    }

    def fake_post(inst, body):
        a = answers[inst]
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(cobalt, "_post", fake_post)
    monkeypatch.setattr(cobalt, "_open", lambda url, timeout=60: FakeResp(b"MP4DATA" * 10, {"Content-Type": "video/mp4"}))
    path = cobalt.download(YT, "medium", str(tmp_path))
    assert path.name == "video.mp4" and path.read_bytes() == b"MP4DATA" * 10


def test_expired_link_tries_next_server(monkeypatch, tmp_path):
    monkeypatch.setattr(cobalt, "_post", lambda inst, body: {"status": "tunnel", "url": f"https://{inst[8:]}/t", "filename": "a.mp4"})
    calls = []

    def fake_open(url, timeout=60):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("HTTP Error 404: link expired")
        return FakeResp(b"OK", {})
    monkeypatch.setattr(cobalt, "_open", fake_open)
    assert cobalt.download(YT, "low", str(tmp_path)).read_bytes() == b"OK"
    assert len(calls) == 2


def test_audio_gets_audio_extension(monkeypatch, tmp_path):
    monkeypatch.setattr(cobalt, "_post", lambda inst, body: {"status": "tunnel", "url": "https://s/t", "filename": ""})
    monkeypatch.setattr(cobalt, "_open", lambda url, timeout=60: FakeResp(b"AUDIO", {"Content-Type": "audio/mp4"}))
    assert cobalt.download(YT, "audio", str(tmp_path)).suffix == ".m4a"


def test_refuses_non_https_links(monkeypatch, tmp_path):
    monkeypatch.setattr(cobalt, "_post", lambda inst, body: {"status": "redirect", "url": "http://plain/x.mp4"})
    monkeypatch.setattr(cobalt, "_open", lambda url, timeout=60: pytest.fail("must not fetch http links"))
    with pytest.raises(core.UserError) as exc:
        cobalt.download(YT, "medium", str(tmp_path))
    assert exc.value.status == 503


def test_size_limit_stops_immediately(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_FILESIZE_MB", "1")
    monkeypatch.setattr(cobalt, "_post", lambda inst, body: {"status": "tunnel", "url": "https://s/t", "filename": "a.mp4"})
    opened = []
    monkeypatch.setattr(cobalt, "_open", lambda url, timeout=60: opened.append(url) or FakeResp(b"x" * (2 * 1024 * 1024), {}))
    with pytest.raises(core.UserError) as exc:
        cobalt.download(YT, "best", str(tmp_path))
    assert exc.value.status == 413 and len(opened) == 1   # no point asking other servers


def test_all_servers_failing_gives_a_clear_message(monkeypatch, tmp_path):
    monkeypatch.setattr(cobalt, "_post", lambda inst, body: (_ for _ in ()).throw(cobalt.CobaltError("down")))
    with pytest.raises(core.UserError) as exc:
        cobalt.download(YT, "medium", str(tmp_path))
    assert exc.value.status == 503 and "backup" in exc.value.message


# ---- oEmbed details ----------------------------------------------------------------
def test_youtube_details(monkeypatch):
    seen = []
    body = json.dumps({"title": "Me at the zoo", "author_name": "jawed", "thumbnail_url": "https://i.ytimg.com/vi/x/hq.jpg"}).encode()
    monkeypatch.setattr(cobalt, "_open", lambda url, timeout=60: seen.append(url) or FakeResp(body))
    assert cobalt.youtube_details(YT) == {"title": "Me at the zoo", "uploader": "jawed", "thumbnail": "https://i.ytimg.com/vi/x/hq.jpg"}
    assert seen[0].startswith("https://www.youtube.com/oembed?format=json&url=https%3A%2F%2Fwww.youtube.com")


def test_youtube_details_failure_is_harmless(monkeypatch):
    monkeypatch.setattr(cobalt, "_open", lambda url, timeout=60: (_ for _ in ()).throw(OSError("nope")))
    assert cobalt.youtube_details(YT) == {}


# ---- wired into the downloader ------------------------------------------------------
def test_blocked_youtube_lookup_uses_oembed(monkeypatch):
    install_fake_ytdlp(monkeypatch, lambda o: (_ for _ in ()).throw(FakeDownloadError(BOT)))
    monkeypatch.setattr(cobalt, "youtube_details", lambda url: {"title": "Me at the zoo", "uploader": "jawed", "thumbnail": None})
    info = ytdl.get_info(YT, "medium")
    assert info["title"] == "Me at the zoo" and info["site"] == "YouTube" and info["uploader"] == "jawed"


def test_blocked_youtube_download_uses_backup(monkeypatch, tmp_path):
    install_fake_ytdlp(monkeypatch, lambda o: (_ for _ in ()).throw(FakeDownloadError(BOT)))
    target = tmp_path / "video.mp4"
    monkeypatch.setattr(cobalt, "download", lambda url, quality, workdir: (target.write_bytes(b"x"), target)[1])
    assert ytdl.download(YT, "medium", str(tmp_path)) == target


def test_backup_off_keeps_the_original_message(monkeypatch):
    monkeypatch.setenv("COBALT_INSTANCES", "off")
    install_fake_ytdlp(monkeypatch, lambda o: (_ for _ in ()).throw(FakeDownloadError(BOT)))
    with pytest.raises(core.UserError) as exc:
        ytdl.get_info(YT)
    assert "bot" in exc.value.message


def test_other_sites_never_use_backup(monkeypatch):
    install_fake_ytdlp(monkeypatch, lambda o: (_ for _ in ()).throw(FakeDownloadError("ERROR: [facebook] x: not a bot check really")))
    monkeypatch.setattr(cobalt, "youtube_details", lambda url: pytest.fail("backup must be YouTube-only"))
    with pytest.raises(core.UserError):
        ytdl.get_info("https://www.facebook.com/watch/?v=1")
