"""vidfetch web app (Starlette / ASGI).

Flow:
  POST /api/prepare  -> checks access key + link, looks the video up, returns a
                        short-lived signed download link
  GET  /api/file?t=  -> downloads the video and streams it back, then deletes it
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import shutil
import tempfile
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import core, ytdl

log = logging.getLogger("vidfetch")
PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"

_active = 0  # downloads running in this process right now


async def health(request: Request):
    return JSONResponse(
        {
            "ok": True,
            "configured": core.server_configured(),
            "locked": core.access_locked(),
            "max_minutes": core.max_duration_seconds() // 60,
            "max_mb": core.max_filesize_bytes() // (1024 * 1024),
        }
    )


async def prepare(request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except Exception:
        raise core.UserError("Bad request.") from None

    if not core.check_access_key(str(body.get("key", ""))):
        await asyncio.sleep(1)  # slow down guessing
        raise core.UserError("This page needs its private link. Open the link you were given.", 401)

    url = core.validate_url(core.extract_url(str(body.get("url", ""))))
    quality = core.parse_quality(body.get("quality"))

    info = await run_in_threadpool(ytdl.get_info, url, quality)
    duration = info.get("duration")
    limit = core.max_duration_seconds()
    if duration and duration > limit:
        raise core.UserError(
            f"That video is {int(duration // 60)} minutes long. The limit is {limit // 60} minutes.", 413
        )

    size = info.get("size_bytes")
    if size and size > core.max_filesize_bytes():
        raise core.UserError(
            f"At this quality the file would be about {size // (1024 * 1024)} MB, "
            f"over the {core.max_filesize_bytes() // (1024 * 1024)} MB limit. Try a lower quality.",
            413,
        )

    token = core.sign_token({"u": url, "q": quality, "n": str(info["title"])[:80]})
    return JSONResponse({**info, "quality": quality, "download_url": f"/api/file?t={token}"})


async def file(request: Request):
    global _active
    payload = core.verify_token(request.query_params.get("t", ""))
    if _active >= core.max_concurrent():
        raise core.UserError("Busy with other downloads. Try again in a minute.", 429)

    workdir = tempfile.mkdtemp(prefix="vidfetch-")
    _active += 1
    try:
        path = await run_in_threadpool(ytdl.download, payload["u"], payload["q"], workdir)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    finally:
        _active -= 1

    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=core.safe_filename(payload.get("n", "video"), path.suffix),
        background=BackgroundTask(shutil.rmtree, workdir, ignore_errors=True),
    )


def _error_response(request: Request, message: str, status: int):
    # A browser that followed a download link wants a readable page, not JSON.
    if "text/html" in request.headers.get("accept", ""):
        page = (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<body style='font:18px system-ui;padding:24px;max-width:32rem;margin:auto'>"
            f"<h2>Download failed</h2><p>{_escape(message)}</p><p><a href='/'>&larr; Back</a></p>"
        )
        return HTMLResponse(page, status_code=status)
    return JSONResponse({"error": message}, status_code=status)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def user_error_handler(request: Request, exc: core.UserError):
    return _error_response(request, exc.message, exc.status)


async def server_error_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    return _error_response(request, "Something went wrong on our side. Try again.", 500)


app = Starlette(
    routes=[
        Route("/api/health", health),
        Route("/api/prepare", prepare, methods=["POST"]),
        Route("/api/file", file),
        # Everything in public/ (page, manifest, icons). On Vercel the CDN serves these directly.
        Mount("/", app=StaticFiles(directory=PUBLIC_DIR, html=True), name="public"),
    ],
    exception_handlers={core.UserError: user_error_handler, Exception: server_error_handler},
)
