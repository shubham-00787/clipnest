"""The smoke script's sample picker, against a fake yt-dlp extractor list."""
import importlib.util
import pathlib
import sys
import types

spec = importlib.util.spec_from_file_location("smoke", pathlib.Path(__file__).parent.parent / "scripts" / "smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def fake_ie(key, cases):
    return type(key + "IE", (), {
        "ie_key": classmethod(lambda cls: key),
        "get_testcases": classmethod(lambda cls, include_onlymatching=False:
                                     [c for c in cases if include_onlymatching or not c.get("only_matching")]),
    })


def test_picks_one_usable_link_per_platform(monkeypatch):
    extractors = [
        fake_ie("Youtube", [
            {"url": "https://www.youtube.com/watch?v=skipme", "skip": "gone"},
            {"url": "https://www.youtube.com/watch?v=good"},
            {"url": "https://www.youtube.com/shorts/short1", "only_matching": True},
        ]),
        fake_ie("Instagram", [{"url": "https://www.instagram.com/p/abc/"}, {"url": "https://www.instagram.com/reel/def/"}]),
        fake_ie("Facebook", [
            {"url": "https://www.facebook.com/x/videos/1/", "expected_warnings": ["Cannot parse data"]},
            {"url": "https://www.facebook.com/x/videos/2/"},
        ]),
        fake_ie("SnapchatSpotlight", [{"url": "https://www.snapchat.com/spotlight/xyz"}]),
    ]
    mod = types.ModuleType("yt_dlp.extractor")
    mod.gen_extractor_classes = lambda: extractors
    monkeypatch.setitem(sys.modules, "yt_dlp", types.ModuleType("yt_dlp"))
    monkeypatch.setitem(sys.modules, "yt_dlp.extractor", mod)

    picked = dict(smoke.ytdlp_sample_links())
    assert picked["youtube"] == ["https://www.youtube.com/watch?v=good"]     # skipped case ignored
    assert picked["youtube-shorts"] == ["https://www.youtube.com/shorts/short1"]
    assert picked["instagram"] == ["https://www.instagram.com/p/abc/", "https://www.instagram.com/reel/def/"]
    assert picked["facebook"] == ["https://www.facebook.com/x/videos/2/"]    # known-broken example left out
    assert picked["snapchat"] == ["https://www.snapchat.com/spotlight/xyz"]
    assert "x" not in picked                                                  # no extractor -> skipped, no crash
