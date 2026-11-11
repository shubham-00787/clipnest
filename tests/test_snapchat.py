"""Snapchat Spotlight fallback: page parsing, and the downloader using it when
yt-dlp's own Snapchat reader fails."""
import json
import sys

import pytest

from app import core, snapchat, ytdl
from tests.test_ytdl_run import FakeDownloadError, install_fake_ytdlp

VID = "W7_EDlXWTBiXAEEniNoMPwAAYZHljeHp0cXJrAZ5cwvr2AZ5cwq33AAAAAQ"
OTHER = "W7_EDlXWTBiXAEEniNoMPwAAYdHZkb3F4Z2VtAaFciRidAaFciQnIAAAAAQ"


def video_obj(raw_id, name):
    return {"id": "35::x::1", "rawSnapId": raw_id, "creatorName": name, "postingUsername": name.lower(),
            "uploadTimestampMs": "1779672722935",
            "posterUrl": f"https://bolt-gcdn.sc-cdn.net/y/{name}.256.IRZXSOY?mo=a",
            "videoUrl": f"https://bolt-gcdn.sc-cdn.net/y/{name}.27.IRZXSOY?mo=b"}


def page_with(*objs, unavailable=False):
    """Mimics the current snapchat.com page: data inside self.__next_f.push([1, "<json string>"])."""
    payload = '5:["$","main",null,{"upNext":{"items":[' + ",".join(
        json.dumps({"tile": {"title": o["creatorName"]}, "video": o}) for o in objs) + "]}}]"
    resolved = ' data-resolved="unavailable"' if unavailable else ""
    return (f'<html><section{resolved}></section><script>self.__next_f.push([1,{json.dumps(payload[:40])}])</script>'
            f'<script>self.__next_f.push([1,{json.dumps(payload[40:])}])</script></html>')  # split across chunks on purpose


def test_spotlight_id():
    assert snapchat.spotlight_id(f"https://www.snapchat.com/spotlight/{VID}") == VID
    assert snapchat.spotlight_id(f"https://www.snapchat.com/@someone/spotlight/{VID}?share=1") == VID
    assert snapchat.spotlight_id("https://www.snapchat.com/add/someone") is None


def test_finds_the_requested_video_among_others():
    v = snapchat.find_video(page_with(video_obj(OTHER, "Ana"), video_obj(VID, "Jemma")), VID)
    assert v["url"].endswith("Jemma.27.IRZXSOY?mo=b")
    assert v["title"] == "Jemma" and v["uploader"] == "Jemma" and v["timestamp"] == 1779672722.935


def test_unavailable_snap():
    assert snapchat.find_video(page_with(video_obj(OTHER, "Ana"), unavailable=True), VID) == {"unavailable": True}


def test_rejects_media_links_off_snapchat_cdn():
    bad = video_obj(VID, "Jemma")
    bad["videoUrl"] = "https://evil.example/x.mp4"
    assert snapchat.find_video(page_with(bad), VID) is None


@pytest.mark.parametrize("ytdlp_error", [
    "ERROR: [SnapchatSpotlight] x: Unable to extract next.js data",
    "ERROR: No suitable extractor found for URL https://www.snapchat.com/@jemma/spotlight/x",
])
def test_downloader_uses_fallback_when_ytdlp_reader_breaks(monkeypatch, ytdlp_error):
    def behaviour(opts):
        raise FakeDownloadError(ytdlp_error)
    links = []
    calls = install_fake_ytdlp(monkeypatch, behaviour, links)
    monkeypatch.setattr(ytdl, "_fetch_page", lambda url: page_with(video_obj(VID, "Jemma")))
    info = ytdl.get_info(f"https://www.snapchat.com/@jemma/spotlight/{VID}")
    assert links[0] == f"https://www.snapchat.com/spotlight/{VID}"   # yt-dlp got the form it recognises
    assert info["title"] == "Jemma" and info["uploader"] == "Jemma" and info["site"] == "Snapchat"
    processed = calls[-1]
    assert processed["download"] is False
    fmt = processed["processed"]["formats"][0]
    assert fmt["ext"] == "mp4" and fmt["url"].endswith("Jemma.27.IRZXSOY?mo=b")   # explicit mp4, not ".IRZXSOY"


def test_snapchat_download_goes_through_fallback(monkeypatch, tmp_path):
    calls = install_fake_ytdlp(monkeypatch, lambda o: (_ for _ in ()).throw(
        FakeDownloadError("ERROR: No suitable extractor found for URL x")))
    monkeypatch.setattr(ytdl, "_fetch_page", lambda url: page_with(video_obj(VID, "Jemma")))
    (tmp_path / "video.mp4").write_bytes(b"x" * 10)   # what yt-dlp would have written
    path = ytdl.download(f"https://www.snapchat.com/spotlight/{VID}", "medium", str(tmp_path))
    assert path.name == "video.mp4" and calls[-1]["download"] is True


def test_downloader_reports_unavailable_snap(monkeypatch):
    install_fake_ytdlp(monkeypatch, lambda o: (_ for _ in ()).throw(
        FakeDownloadError("ERROR: [SnapchatSpotlight] x: Unable to extract next.js data")))
    monkeypatch.setattr(ytdl, "_fetch_page", lambda url: page_with(unavailable=True))
    with pytest.raises(core.UserError) as exc:
        ytdl.get_info(f"https://www.snapchat.com/spotlight/{VID}")
    assert "isn't available" in exc.value.message


def test_has_no_sound():
    assert ytdl.has_no_sound({"formats": [{"acodec": "none"}, {"acodec": "none"}]}) is True
    assert ytdl.has_no_sound({"formats": [{"acodec": "none"}, {"acodec": None}]}) is False   # unknown: don't claim
    assert ytdl.has_no_sound({"formats": [{"acodec": "mp4a.40.2"}]}) is False
    assert ytdl.has_no_sound({}) is False
