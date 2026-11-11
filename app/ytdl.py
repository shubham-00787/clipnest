"""Thin wrapper around yt-dlp. yt_dlp is imported lazily so the rest of the app
(and the tests) work without it installed."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import core, snapchat

_SKIP_SUFFIXES = {".part", ".ytdl", ".temp", ".json", ".txt", ".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt"}
_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def is_youtube(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _YOUTUBE_HOSTS)


def _ffmpeg_path() -> str | None:
    if shutil.which("ffmpeg"):
        return None  # yt-dlp finds it on PATH
    try:  # pip-installable static binary, used on Vercel where there is no apt
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _cookiefile() -> str | None:
    """COOKIES_TXT holds the contents of a Netscape-format cookies.txt."""
    content = os.environ.get("COOKIES_TXT")
    if not content:
        return None
    path = os.path.join(tempfile.gettempdir(), "vidfetch-cookies.txt")
    try:
        existing = Path(path).read_text() if os.path.exists(path) else None
        if existing != content:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(content)
    except OSError:
        return None
    return path


def base_opts(url: str) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 1,
    }
    if os.environ.get("ALLOW_GENERIC") != "1":
        # Only sites yt-dlp has a dedicated extractor for. This also stops the
        # "generic" extractor from fetching arbitrary URLs for strangers.
        opts["allowed_extractors"] = ["default", "-generic"]
    ffmpeg = _ffmpeg_path()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    proxy = os.environ.get("YTDLP_PROXY")
    if proxy and (os.environ.get("PROXY_ALL") == "1" or is_youtube(url)):
        opts["proxy"] = proxy
    cookies = _cookiefile()
    if cookies:
        opts["cookiefile"] = cookies
    deno = _deno_path()
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    return opts


def _deno_path() -> str | None:
    """Where the Deno JavaScript runtime is (YouTube needs one).

    DENO_PATH wins; a deno on PATH is found by yt-dlp itself; otherwise use the
    copy the official `deno` pip package ships (that's how it gets onto Vercel,
    which only installs Python packages)."""
    if os.environ.get("DENO_PATH"):
        return os.environ["DENO_PATH"]
    if shutil.which("deno"):
        return None
    try:
        import deno

        return deno.find_deno_bin()
    except Exception:
        return None


# yt-dlp messages that mean "the site's page changed and the dedicated
# extractor can't read it": worth one retry with the generic page reader.
_PARSE_ERRORS = ("unable to extract", "cannot parse data", "unable to parse", "no video formats found")


def friendly_error(message: str) -> str:
    low = message.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube blocked this server (it thinks we're a bot). "
                "Try again later, or ask the app owner to add cookies or a proxy.")
    if "unsupported url" in low:
        return "That site or link type isn't supported."
    if "authentication is required" in low or "--cookies" in low:
        return "This site only allows downloads for logged-in accounts. The app owner can add cookies to enable it."
    if any(s in low for s in ("login required", "log in", "requiring login", "private", "rate-limit reached")):
        return "That post is private or needs a login, so it can't be downloaded."
    if any(s in low for s in ("timed out", "domain not found", "name or service not known",
                              "nodename nor servname", "connection refused", "network is unreachable")):
        return "Couldn't reach that website. It may be down, or blocked on this network or in your country."
    if any(s in low for s in _PARSE_ERRORS):
        return "That site changed its page and the downloader can't read it right now. Try again in a few days."
    if any(s in low for s in ("unavailable", "removed", "deleted", "not available", "no video")):
        return "That video isn't available (it may be deleted, private or region-locked)."
    if "429" in low or "too many requests" in low:
        return "The site is limiting us right now. Try again in a few minutes."
    return "Couldn't fetch that link. Make sure it's a public video and try again."


def has_no_sound(info: dict) -> bool:
    """True only when every format is explicitly marked as having no audio.
    Unknown audio (None) doesn't count, since many sites don't say."""
    formats = info.get("formats") or []
    return bool(formats) and all(f.get("acodec") == "none" for f in formats)


def estimate_bytes(info: dict) -> int | None:
    """Best guess of the download size from the formats yt-dlp picked, or None
    when the site doesn't say."""
    formats = info.get("requested_formats") or [info]
    sizes = [f.get("filesize") or f.get("filesize_approx") for f in formats]
    if sizes and all(sizes):
        return int(sum(sizes))
    return None


_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def _fetch_page(url: str) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.read(5_000_000).decode("utf-8", "replace")


def _run(url: str, opts: dict, download: bool):
    """Look up (download=False) or download (download=True) one video.

    When yt-dlp's site-specific reader fails because the site's page changed:
    - Snapchat Spotlight: read the video from the current page format ourselves
      (app/snapchat.py) and hand yt-dlp a ready-made description of the MP4.
    - Anything else except YouTube: retry once with yt-dlp's generic page
      reader (it finds og:video / JSON-LD video links many sites publish).
    """
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    def act(ydl, link):
        return ydl.download([link]) if download else ydl.extract_info(link, download=False)

    snap_id = snapchat.spotlight_id(url)
    # yt-dlp only recognises snapchat.com/spotlight/<id>, not the newer @user/spotlight/<id> links
    first_link = snapchat.canonical_url(snap_id) if snap_id else url
    try:
        with YoutubeDL(opts) as ydl:
            return act(ydl, first_link)
    except DownloadError as exc:
        first = str(exc)
    low = first.lower()

    if snap_id and (any(p in low for p in _PARSE_ERRORS) or "no suitable extractor" in low or "unsupported url" in low):
        return _snapchat_fallback(url, snap_id, opts, download, first)
    if not any(p in low for p in _PARSE_ERRORS) or is_youtube(url):
        raise core.UserError(friendly_error(first), 422)

    retry = dict(opts, force_generic_extractor=True)
    retry.pop("allowed_extractors", None)
    try:
        with YoutubeDL(retry) as ydl:
            return act(ydl, url)
    except DownloadError:
        raise core.UserError(friendly_error(first), 422) from None


def _snapchat_fallback(url: str, snap_id: str, opts: dict, download: bool, first_error: str):
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    try:
        video = snapchat.find_video(_fetch_page(url), snap_id)
    except OSError:
        video = None
    if video and video.get("unavailable"):
        raise core.UserError("That Snap isn't available on the web. It may be deleted or private.", 422)
    if not video:
        raise core.UserError(friendly_error(first_error), 422)
    # Snapchat's media links end in ".IRZXSOY", not ".mp4", so describe the file
    # explicitly instead of letting yt-dlp guess (it refuses unknown extensions).
    entry = {
        "id": snap_id,
        "title": video["title"],
        "uploader": video.get("uploader"),
        "thumbnail": video.get("thumbnail"),
        "timestamp": video.get("timestamp"),
        "webpage_url": url,
        "extractor": "snapchat:web",
        "extractor_key": "Snapchat",
        "formats": [{"format_id": "web", "url": video["url"], "ext": "mp4",
                     "http_headers": {"User-Agent": _BROWSER_UA}}],
    }
    try:
        with YoutubeDL(opts) as ydl:
            return ydl.process_ie_result(entry, download=download)
    except DownloadError as exc:
        raise core.UserError(friendly_error(str(exc)), 422) from None


def get_info(url: str, quality: str = core.DEFAULT_QUALITY) -> dict:
    """Look the video up without downloading it, using the same format choice
    the download will use so the size estimate matches."""
    opts = base_opts(url)
    opts.update(core.format_options(quality))
    info = _run(url, opts, download=False)
    if info is None:
        raise core.UserError(friendly_error(""), 422)
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise core.UserError("No video found at that link.", 422)
        info = entries[0]
    return {
        "title": info.get("title") or "video",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader") or info.get("channel"),
        "site": info.get("extractor_key") or info.get("extractor"),
        "height": info.get("height"),
        "size_bytes": estimate_bytes(info),
        # the site itself says no stream has sound (Instagram reports this per video)
        "no_sound": quality != "audio" and has_no_sound(info),
    }


def download(url: str, quality: str, workdir: str) -> Path:
    """Download into workdir and return the finished file."""
    opts = base_opts(url)
    opts.update(core.format_options(quality))
    opts.update({"outtmpl": os.path.join(workdir, "video.%(ext)s"), "max_filesize": core.max_filesize_bytes()})
    if quality != "audio":
        opts["merge_output_format"] = "mp4"
    _run(url, opts, download=True)

    files = [p for p in Path(workdir).iterdir() if p.is_file() and p.suffix.lower() not in _SKIP_SUFFIXES]
    if not files:
        raise core.UserError(
            f"Nothing was downloaded. The video may be bigger than the {core.max_filesize_bytes() // (1024 * 1024)} MB limit.",
            413,
        )
    result = max(files, key=lambda p: p.stat().st_size)
    if result.stat().st_size > core.max_filesize_bytes():
        raise core.UserError("That video is bigger than the size limit.", 413)
    return result
