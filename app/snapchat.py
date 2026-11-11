"""Fallback reader for Snapchat Spotlight pages.

yt-dlp's Snapchat extractor reads a `__NEXT_DATA__` block that Snapchat's
website no longer serves. The current page streams its data as Next.js
"flight" chunks (`self.__next_f.push([1, "..."])`), where each video appears as
an object with `rawSnapId`, `videoUrl`, `posterUrl`, `creatorName` and so on.
This module finds the object for the requested Spotlight in that data.
Pure functions only: fetching is done by the caller.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

SPOTLIGHT_RE = re.compile(r"https?://(?:www\.)?snapchat\.com/(?:@[^/?#]+/)?spotlight/(?P<id>[\w-]{20,})", re.I)
_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', re.S)
_MEDIA_HOST_SUFFIX = ".sc-cdn.net"


def spotlight_id(url: str) -> str | None:
    m = SPOTLIGHT_RE.match(url or "")
    return m.group("id") if m else None


def canonical_url(video_id: str) -> str:
    """The link form yt-dlp's own Snapchat reader recognises."""
    return f"https://www.snapchat.com/spotlight/{video_id}"


def _flight_text(page: str) -> str:
    parts = []
    for literal in _PUSH_RE.findall(page):
        try:
            parts.append(json.loads(literal))
        except ValueError:
            continue
    return "".join(parts)


def _is_snapchat_media(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").endswith(_MEDIA_HOST_SUFFIX)


def find_video(page: str, video_id: str) -> dict | None:
    """Return {"url", "title", "uploader", "thumbnail", "timestamp"} for the
    Spotlight `video_id`, {"unavailable": True} if the page says it's gone,
    or None if nothing usable was found."""
    text = _flight_text(page)
    decoder = json.JSONDecoder()
    for match in re.finditer(r'"rawSnapId"\s*:\s*"' + re.escape(video_id) + '"', text):
        pos = match.start()
        # walk back to each enclosing "{" and try to parse the object there
        start = pos
        for _ in range(40):
            start = text.rfind("{", 0, start)
            if start == -1:
                break
            try:
                obj, _end = decoder.raw_decode(text, start)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("rawSnapId") == video_id:
                if _is_snapchat_media(obj.get("videoUrl")):
                    ts = obj.get("uploadTimestampMs")
                    return {
                        "url": obj["videoUrl"],
                        "title": obj.get("title") or obj.get("creatorName") or "Snapchat Spotlight",
                        "uploader": obj.get("creatorName") or obj.get("postingUsername"),
                        "thumbnail": obj.get("posterUrl") if _is_snapchat_media(obj.get("posterUrl")) else None,
                        "timestamp": int(ts) / 1000 if str(ts or "").isdigit() else None,
                    }
                break
    if 'data-resolved="unavailable"' in page:
        return {"unavailable": True}
    return None
