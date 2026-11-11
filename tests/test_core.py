import socket

import pytest

from app import core, ytdl


def fake_dns(ip):
    def _getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    return _getaddrinfo


# ---- extract_url -----------------------------------------------------------
def test_extract_url_from_share_text():
    text = "Look at this!! https://www.facebook.com/watch/?v=123456. so funny"
    assert core.extract_url(text) == "https://www.facebook.com/watch/?v=123456"


def test_extract_url_bare_domain():
    assert core.extract_url("youtu.be/abc123") == "https://youtu.be/abc123"


@pytest.mark.parametrize("bad", ["", "hello there", "   "])
def test_extract_url_rejects_non_links(bad):
    with pytest.raises(core.UserError):
        core.extract_url(bad)


# ---- validate_url ----------------------------------------------------------
def test_validate_url_accepts_public(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns("142.250.80.46"))
    assert core.validate_url("https://www.youtube.com/watch?v=x") == "https://www.youtube.com/watch?v=x"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1"])
def test_validate_url_blocks_private(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns(ip))
    with pytest.raises(core.UserError):
        core.validate_url("https://evil.example/video")


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)", "https://"])
def test_validate_url_blocks_bad_schemes(monkeypatch, url):
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns("142.250.80.46"))
    with pytest.raises(core.UserError):
        core.validate_url(url)


def test_validate_url_blocks_odd_port(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns("142.250.80.46"))
    with pytest.raises(core.UserError):
        core.validate_url("https://example.com:8080/x")


def test_validate_url_dns_failure(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(core.UserError):
        core.validate_url("https://nope.invalid/x")


# ---- quality ---------------------------------------------------------------
def test_default_quality_is_medium():
    assert core.parse_quality(None) == "medium"
    assert core.parse_quality("") == "medium"


def test_unknown_quality_rejected():
    with pytest.raises(core.UserError):
        core.parse_quality("ultra")


@pytest.mark.parametrize("quality,res", [("low", "res:360"), ("medium", "res:480"), ("high", "res:720"), ("best", "res")])
def test_video_presets_always_join_sound_and_sort_by_shorter_side(quality, res):
    opts = core.format_options(quality)
    assert opts["format"] == "bv*+ba/b"            # picture + separate sound track, or a combined file
    assert opts["format_sort"][0] == res           # "res" = shorter side, so vertical videos aren't tiny
    assert "vcodec:h264" in opts["format_sort"] and "acodec:aac" in opts["format_sort"]


def test_audio_preset():
    assert core.format_options("audio") == {"format": "ba/b", "format_sort": ["acodec:aac"]}


# ---- filenames -------------------------------------------------------------
def test_safe_filename():
    assert core.safe_filename('A/B: "cool" video?', ".mp4") == "A B cool video.mp4"
    assert core.safe_filename("", "mp4") == "video.mp4"
    assert len(core.safe_filename("x" * 500, ".mp4")) <= 84


# ---- access_key + tokens -----------------------------------------------------
def test_access_key_fails_closed(monkeypatch):
    monkeypatch.delenv("ACCESS_KEY", raising=False)
    monkeypatch.delenv("VIDFETCH_OPEN", raising=False)
    with pytest.raises(core.UserError) as exc:
        core.check_access_key("anything")
    assert exc.value.status == 503


def test_access_key_open_mode(monkeypatch):
    monkeypatch.delenv("ACCESS_KEY", raising=False)
    monkeypatch.setenv("VIDFETCH_OPEN", "1")
    assert core.check_access_key("") is True


def test_access_key_check(monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "hunter2")
    assert core.check_access_key("hunter2") is True
    assert core.check_access_key("hunter3") is False
    assert core.check_access_key(None) is False


def test_token_roundtrip_and_tamper(monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "pw")
    token = core.sign_token({"u": "https://x.test/v", "q": "low"}, now=1000)
    assert core.verify_token(token, now=1001)["u"] == "https://x.test/v"
    body, sig = token.split(".")
    with pytest.raises(core.UserError):
        core.verify_token(body + "." + sig[:-2] + "AA", now=1001)
    with pytest.raises(core.UserError):
        core.verify_token("garbage", now=1001)


def test_token_expires(monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "pw")
    token = core.sign_token({"u": "x"}, now=1000)
    with pytest.raises(core.UserError) as exc:
        core.verify_token(token, now=1000 + core.TOKEN_TTL_SECONDS + 1)
    assert exc.value.status == 410


def test_token_not_valid_under_other_secret(monkeypatch):
    monkeypatch.setenv("ACCESS_KEY", "one")
    token = core.sign_token({"u": "x"})
    monkeypatch.setenv("ACCESS_KEY", "two")
    with pytest.raises(core.UserError):
        core.verify_token(token)


# ---- ytdl helpers (no yt-dlp needed) ---------------------------------------
@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=x", True),
    ("https://youtu.be/x", True),
    ("https://m.youtube.com/shorts/x", True),
    ("https://notyoutube.com/x", False),
    ("https://instagram.com/reel/x", False),
])
def test_is_youtube(url, expected):
    assert ytdl.is_youtube(url) is expected


def test_proxy_only_for_youtube(monkeypatch):
    monkeypatch.setenv("YTDLP_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.delenv("PROXY_ALL", raising=False)
    assert ytdl.base_opts("https://youtu.be/x")["proxy"] == "socks5://127.0.0.1:1080"
    assert "proxy" not in ytdl.base_opts("https://instagram.com/reel/x")
    monkeypatch.setenv("PROXY_ALL", "1")
    assert "proxy" in ytdl.base_opts("https://instagram.com/reel/x")


def test_generic_extractor_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_GENERIC", raising=False)
    assert "-generic" in ytdl.base_opts("https://x.test/")["allowed_extractors"]
    monkeypatch.setenv("ALLOW_GENERIC", "1")
    assert "allowed_extractors" not in ytdl.base_opts("https://x.test/")


@pytest.mark.parametrize("raw,expect", [
    ("ERROR: Sign in to confirm you're not a bot", "bot"),
    ("ERROR: login required", "private"),
    ("ERROR: Unsupported URL: https://x", "isn't supported"),
    # these are the real messages from the first live run
    ("ERROR: [Reddit] 6rrwyj: Account authentication is required. Use --cookies-from-browser or --cookies", "logged-in"),
    ("ERROR: [TikTok] 674: Unable to download webpage: Connection to www.tiktok.com timed out. (connect timeout=20.0)", "blocked"),
    ("ERROR: [twitter:amplify] 93a: Unable to download webpage: HTTP Error 500: Domain Not Found", "Couldn't reach"),
    ("ERROR: [facebook] 367: Cannot parse data; please report this issue", "changed its page"),
    ("ERROR: [SnapchatSpotlight] W7_: Unable to extract next.js data; please report this issue", "changed its page"),
])
def test_friendly_errors(raw, expect):
    assert expect in ytdl.friendly_error(raw)


def test_estimate_bytes():
    # video + audio streams, both sizes known
    assert ytdl.estimate_bytes({"requested_formats": [{"filesize": 8_000_000}, {"filesize_approx": 1_000_000}]}) == 9_000_000
    # a single combined format
    assert ytdl.estimate_bytes({"filesize_approx": 5_000_000}) == 5_000_000
    # one stream has no size -> we don't guess
    assert ytdl.estimate_bytes({"requested_formats": [{"filesize": 8_000_000}, {}]}) is None
    assert ytdl.estimate_bytes({}) is None


def test_deno_from_pip_package_when_not_on_path(monkeypatch):
    import sys, types
    monkeypatch.delenv("DENO_PATH", raising=False)
    monkeypatch.setattr(ytdl.shutil, "which", lambda name: None)
    fake = types.ModuleType("deno"); fake.find_deno_bin = lambda: "/site-packages/deno/bin/deno"
    monkeypatch.setitem(sys.modules, "deno", fake)
    assert ytdl.base_opts("https://youtu.be/x")["js_runtimes"] == {"deno": {"path": "/site-packages/deno/bin/deno"}}


def test_deno_path_env_wins(monkeypatch):
    monkeypatch.setenv("DENO_PATH", "/opt/deno")
    assert ytdl.base_opts("https://youtu.be/x")["js_runtimes"] == {"deno": {"path": "/opt/deno"}}
