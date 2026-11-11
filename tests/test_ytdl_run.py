"""ytdl.get_info/download against a fake yt_dlp, to check the options we pass
and the one-time retry with the generic page reader."""
import sys
import types

import pytest

from app import core, ytdl


class FakeDownloadError(Exception):
    pass


def install_fake_ytdlp(monkeypatch, behaviour, seen_links=None):
    """behaviour(opts) -> info dict, or raises FakeDownloadError."""
    calls = []
    seen_links = seen_links if seen_links is not None else []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            calls.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            seen_links.append(url)
            return behaviour(self.opts)

        def download(self, urls):
            behaviour(self.opts)

        def process_ie_result(self, info, download=True):
            calls.append({"processed": info, "download": download})
            return dict(info)

    yt = types.ModuleType("yt_dlp")
    yt.YoutubeDL = FakeYDL
    utils = types.ModuleType("yt_dlp.utils")
    utils.DownloadError = FakeDownloadError
    monkeypatch.setitem(sys.modules, "yt_dlp", yt)
    monkeypatch.setitem(sys.modules, "yt_dlp.utils", utils)
    return calls


def test_get_info_passes_quality_options(monkeypatch):
    calls = install_fake_ytdlp(monkeypatch, lambda o: {"title": "t", "height": 480})
    info = ytdl.get_info("https://www.instagram.com/reel/x/", "medium")
    assert info["title"] == "t"
    assert calls[0]["format"] == "bv*+ba/b" and calls[0]["format_sort"][0] == "res:480"


def test_parse_error_retries_once_with_generic_reader(monkeypatch):
    def behaviour(opts):
        if not opts.get("force_generic_extractor"):
            raise FakeDownloadError("ERROR: [SnapchatSpotlight] x: Unable to extract next.js data")
        return {"title": "from generic"}
    calls = install_fake_ytdlp(monkeypatch, behaviour)
    info = ytdl.get_info("https://www.snapchat.com/spotlight/abc")
    assert info["title"] == "from generic"
    assert len(calls) == 2
    assert "allowed_extractors" in calls[0] and "allowed_extractors" not in calls[1]
    assert calls[1]["force_generic_extractor"] is True


def test_no_retry_for_other_errors(monkeypatch):
    def behaviour(opts):
        raise FakeDownloadError("ERROR: [Reddit] x: Account authentication is required. Use --cookies")
    calls = install_fake_ytdlp(monkeypatch, behaviour)
    with pytest.raises(core.UserError) as exc:
        ytdl.get_info("https://www.reddit.com/r/videos/comments/abc/")
    assert len(calls) == 1 and "logged-in" in exc.value.message


def test_no_generic_retry_for_youtube(monkeypatch):
    def behaviour(opts):
        raise FakeDownloadError("ERROR: [youtube] x: Unable to extract player response")
    calls = install_fake_ytdlp(monkeypatch, behaviour)
    with pytest.raises(core.UserError):
        ytdl.get_info("https://www.youtube.com/watch?v=x")
    assert len(calls) == 1


def test_failed_retry_reports_the_original_reason(monkeypatch):
    def behaviour(opts):
        raise FakeDownloadError("ERROR: [facebook] x: Cannot parse data" if not opts.get("force_generic_extractor")
                                else "ERROR: Unsupported URL")
    install_fake_ytdlp(monkeypatch, behaviour)
    with pytest.raises(core.UserError) as exc:
        ytdl.get_info("https://www.facebook.com/watch/?v=1")
    assert "changed its page" in exc.value.message


def test_download_uses_quality_and_mp4(monkeypatch, tmp_path):
    def behaviour(opts):
        (tmp_path / "video.mp4").write_bytes(b"x" * 10)
    calls = install_fake_ytdlp(monkeypatch, behaviour)
    path = ytdl.download("https://www.facebook.com/reel/1", "low", str(tmp_path))
    assert path.name == "video.mp4"
    assert calls[0]["merge_output_format"] == "mp4" and calls[0]["format_sort"][0] == "res:360"
