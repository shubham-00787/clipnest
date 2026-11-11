"""Pure helpers: settings, URL checks, quality presets, signed download tokens.

Nothing in here touches the network (except DNS in validate_url) or yt-dlp,
so it is cheap to import and easy to test.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import time
from urllib.parse import urlsplit


class UserError(Exception):
    """An error whose message is safe and useful to show to the person."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# quality name -> max height in pixels (None = no height cap)
QUALITIES: dict[str, int | None] = {
    "low": 360,
    "medium": 480,
    "high": 720,
    "best": None,
    "audio": None,
}
DEFAULT_QUALITY = "medium"
TOKEN_TTL_SECONDS = 600


# --------------------------------------------------------------------------
# settings (read from the environment on every call so tests can change them)
# --------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def max_duration_seconds() -> int:
    return _env_int("MAX_DURATION_SECONDS", 900)  # 15 minutes


def max_filesize_bytes() -> int:
    return _env_int("MAX_FILESIZE_MB", 200) * 1024 * 1024


def max_concurrent() -> int:
    return max(1, _env_int("MAX_CONCURRENT", 2))


# --------------------------------------------------------------------------
# input handling
# --------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def extract_url(text: str) -> str:
    """Pull the first link out of whatever was pasted (share texts often have
    words around the link)."""
    text = (text or "").strip()
    match = _URL_RE.search(text)
    if match:
        return match.group(0).rstrip(".,;:!?)]}")
    # allow a bare "youtu.be/abc" style paste
    if text and " " not in text and "." in text and "/" in text:
        return "https://" + text
    raise UserError("Paste a video link (it should start with http or https).")


def validate_url(url: str) -> str:
    """Reject anything that isn't a normal public web link."""
    if len(url) > 2048:
        raise UserError("That link is too long.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UserError("That doesn't look like a valid link.")
    try:
        port = parts.port
    except ValueError:
        raise UserError("That doesn't look like a valid link.") from None
    if port not in (None, 80, 443):
        raise UserError("That link uses an unusual port, which isn't allowed.")
    try:
        addresses = socket.getaddrinfo(parts.hostname, None)
    except socket.gaierror:
        raise UserError("Couldn't find that website. Check the link.") from None
    for info in addresses:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise UserError("That link points to a private address, which isn't allowed.")
    return url


def parse_quality(value: str | None) -> str:
    quality = (value or DEFAULT_QUALITY).strip().lower()
    if quality not in QUALITIES:
        raise UserError("Unknown quality option.")
    return quality


def format_options(quality: str) -> dict:
    """yt-dlp options that pick the formats for a quality preset.

    - "bv*+ba/b" always joins a sound track to the picture when the site
      serves them separately (Instagram and Facebook do), so files have sound.
    - "res:N" sorts by the video's *shorter* side, so a vertical 480x854 Reel
      counts as 480p, and falls back to the nearest larger size when nothing
      is that small (instead of grabbing a silent stream).
    - H.264 video and AAC audio are preferred so the MP4 plays on every phone
      and joining needs no re-encoding.
    """
    if quality == "audio":
        return {"format": "ba/b", "format_sort": ["acodec:aac"]}
    height = QUALITIES[quality]
    res = "res" if height is None else f"res:{height}"
    return {"format": "bv*+ba/b", "format_sort": [res, "vcodec:h264", "acodec:aac"]}


_BAD_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(title: str, ext: str) -> str:
    name = _BAD_FILENAME_CHARS.sub(" ", title or "")
    name = re.sub(r"\s+", " ", name).strip(" .")[:80].strip(" .") or "video"
    ext = ext if ext.startswith(".") else "." + ext
    return name + ext


# --------------------------------------------------------------------------
# access key + signed tokens
# --------------------------------------------------------------------------
def access_locked() -> bool:
    """True when visitors need the private link (an ACCESS_KEY is set)."""
    return bool(os.environ.get("ACCESS_KEY"))


def server_configured() -> bool:
    return access_locked() or os.environ.get("VIDFETCH_OPEN") == "1"


def check_access_key(provided: str | None) -> bool:
    """True if the access key is right. Raises 503 if the server has no key
    configured (fails closed unless VIDFETCH_OPEN=1 is set on purpose)."""
    expected = os.environ.get("ACCESS_KEY")
    if not expected:
        if os.environ.get("VIDFETCH_OPEN") == "1":
            return True
        raise UserError(
            "This server isn't set up yet: the ACCESS_KEY setting is missing.",
            503,
        )
    a = hashlib.sha256((provided or "").encode()).digest()
    b = hashlib.sha256(expected.encode()).digest()
    return hmac.compare_digest(a, b)


def _secret() -> bytes:
    key = os.environ.get("SECRET_KEY") or os.environ.get("ACCESS_KEY") or "vidfetch-open"
    return hashlib.sha256(("vidfetch:" + key).encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_token(payload: dict, now: float | None = None) -> str:
    data = dict(payload)
    data["exp"] = int((now if now is not None else time.time()) + TOKEN_TTL_SECONDS)
    body = _b64(json.dumps(data, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str, now: float | None = None) -> dict:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        data = json.loads(_unb64(body))
    except Exception:
        raise UserError("This download link isn't valid. Tap Get video to start again.", 400) from None
    if data.get("exp", 0) < (now if now is not None else time.time()):
        raise UserError("This download link expired. Tap Get video to start again.", 410)
    return data
