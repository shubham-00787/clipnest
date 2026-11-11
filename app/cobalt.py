"""Backup route for YouTube when it blocks our server.

YouTube refuses most requests from cloud servers ("Sign in to confirm you're
not a bot"). When that happens we ask community-run cobalt servers
(https://github.com/imputnet/cobalt, open source) for the file instead, trying
each server in turn. The title and thumbnail come from YouTube's public oEmbed
endpoint, which isn't behind the bot check.

These servers are run by volunteers: they can go down or change, so the list is
a setting (COBALT_INSTANCES, comma-separated; "off" disables the backup). The
official cobalt.tools servers are deliberately not used: they're not meant for
other projects. We identify ourselves honestly and only call them as a fallback.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import core

# Community servers that answered YouTube requests without an API key when
# checked on 11 Nov 2026. Re-check if the backup stops working.
DEFAULT_INSTANCES = (
    "https://cobaltapi.cjs.nz",
    "https://api.cobalt.liubquanti.click",
    "https://rue-cobalt.xenon.zone",
)
USER_AGENT = "vidfetch/1.0 (+https://github.com/shubham-00787/vidfetch)"
_QUALITY = {"low": "360", "medium": "480", "high": "720", "best": "max"}
_EXT_BY_TYPE = {"video/mp4": ".mp4", "audio/mp4": ".m4a", "video/webm": ".webm", "audio/webm": ".webm",
                "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/opus": ".opus"}


class CobaltError(Exception):
    pass


def instances() -> list[str]:
    raw = os.environ.get("COBALT_INSTANCES")
    if raw is None:
        return list(DEFAULT_INSTANCES)
    if raw.strip().lower() in ("", "off", "none", "0"):
        return []
    return [i.strip().rstrip("/") for i in raw.split(",") if i.strip()]


def enabled() -> bool:
    return bool(instances())


def request_body(url: str, quality: str) -> dict:
    body = {"url": url, "filenameStyle": "basic", "localProcessing": "disabled",
            "youtubeVideoCodec": "h264", "youtubeVideoContainer": "mp4"}
    if quality == "audio":
        body.update(downloadMode="audio", audioFormat="best")
    else:
        body["videoQuality"] = _QUALITY.get(quality, "480")
    return body


def _post(instance: str, body: dict, timeout: float = 20) -> dict:
    req = urllib.request.Request(
        instance + "/", data=json.dumps(body).encode(), method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read(1_000_000))
    except urllib.error.HTTPError as exc:  # cobalt puts the reason in a JSON body even on 4xx
        try:
            return json.loads(exc.read(100_000))
        except Exception:
            raise CobaltError(f"HTTP {exc.code}") from None


def _open(url: str, timeout: float = 60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def ask(instance: str, url: str, quality: str) -> tuple[str, str]:
    """Ask one server for the file. Returns (file_url, suggested_filename)."""
    data = _post(instance, request_body(url, quality))
    status = data.get("status")
    if status in ("tunnel", "redirect") and data.get("url"):
        return data["url"], data.get("filename") or ""
    if status == "picker":
        for item in data.get("picker") or []:
            if item.get("type") == "video" and item.get("url"):
                return item["url"], ""
    code = (data.get("error") or {}).get("code") or status or "no usable answer"
    raise CobaltError(str(code))


def _extension(filename: str, content_type: str, quality: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in (".mp4", ".m4a", ".webm", ".mp3", ".ogg", ".opus", ".mkv"):
        return suffix
    ext = _EXT_BY_TYPE.get((content_type or "").split(";")[0].strip().lower())
    return ext or (".m4a" if quality == "audio" else ".mp4")


def _fetch(file_url: str, filename: str, quality: str, workdir: str) -> Path:
    if urllib.parse.urlsplit(file_url).scheme != "https":
        raise CobaltError("refused a non-https file link")
    limit = core.max_filesize_bytes()
    with _open(file_url) as res:
        declared = res.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise core.UserError("That video is bigger than the size limit.", 413)
        path = Path(workdir) / ("video" + _extension(filename, res.headers.get("Content-Type", ""), quality))
        written = 0
        with open(path, "wb") as out:
            while True:
                chunk = res.read(1 << 20)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise core.UserError("That video is bigger than the size limit.", 413)
                out.write(chunk)
    if written == 0:
        path.unlink(missing_ok=True)
        raise CobaltError("empty file")
    return path


def download(url: str, quality: str, workdir: str) -> Path:
    """Try each server until one hands over the file. File links can expire
    within about a minute, so each one is fetched straight away."""
    reasons = []
    for instance in instances():
        try:
            file_url, filename = ask(instance, url, quality)
            return _fetch(file_url, filename, quality, workdir)
        except core.UserError:
            raise  # e.g. over the size limit: another server won't change that
        except Exception as exc:  # dead server, expired link, rate limit, unexpected answer
            reasons.append(f"{urllib.parse.urlsplit(instance).hostname}: {exc}")
    raise core.UserError(
        "YouTube blocked this server, and the backup download services couldn't get the video either. "
        "Try again in a few minutes.", 503)


def youtube_details(url: str) -> dict:
    """Title, channel and thumbnail from YouTube's public oEmbed endpoint
    (works from servers YouTube otherwise blocks). Empty dict if unavailable."""
    endpoint = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
    try:
        with _open(endpoint, timeout=10) as res:
            data = json.loads(res.read(200_000))
    except Exception:
        return {}
    return {"title": data.get("title"), "uploader": data.get("author_name"), "thumbnail": data.get("thumbnail_url")}
