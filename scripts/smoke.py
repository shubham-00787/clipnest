#!/usr/bin/env python3
"""Live check: does each platform really hand over a video through a running vidfetch?

Start the app first (see README), then:

    python3 scripts/smoke.py --base http://localhost:8000 --key change-me --links scripts/links.txt

or pass links directly:

    python3 scripts/smoke.py youtube=https://youtu.be/jNQXAC9IVRw instagram=https://www.instagram.com/reel/XXXX/

Each link goes through the same two calls the web page makes (/api/prepare,
then /api/file). A platform passes when a non-empty file comes back that looks
like a real MP4/M4A. Uses only the Python standard library.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import unquote


def request(url, data=None, timeout=330):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def error_text(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            return json.loads(exc.read()).get("error", f"HTTP {exc.code}")
        except Exception:
            return f"HTTP {exc.code}"
    return f"{type(exc).__name__}: {exc}"


def looks_like_media(path: str) -> bool:
    """MP4 and M4A files carry 'ftyp' at byte 4."""
    with open(path, "rb") as fh:
        head = fh.read(12)
    return len(head) >= 8 and head[4:8] == b"ftyp"


def check(label: str, link: str, base: str, key: str, quality: str, keep_dir: str | None) -> dict:
    row = {"platform": label, "ok": False, "lookup": None, "download": None, "mb": None, "note": ""}
    t0 = time.time()
    try:
        body = json.dumps({"url": link, "quality": quality, "key": key}).encode()
        with request(base + "/api/prepare", body) as res:
            info = json.loads(res.read())
        row["lookup"] = time.time() - t0
    except Exception as exc:
        row["note"] = "lookup failed: " + error_text(exc)
        return row

    t1 = time.time()
    path = os.path.join(keep_dir or tempfile.gettempdir(), f"vidfetch-smoke-{label}.bin")
    try:
        with request(base + info["download_url"]) as res, open(path, "wb") as out:
            while True:
                chunk = res.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
            name = res.headers.get("content-disposition", "")
        row["download"] = time.time() - t1
    except Exception as exc:
        row["note"] = "download failed: " + error_text(exc)
        return row

    size = os.path.getsize(path)
    row["mb"] = size / 1048576
    if size == 0:
        row["note"] = "empty file"
    elif not looks_like_media(path):
        row["note"] = "file is not a valid MP4/M4A"
    else:
        row["ok"] = True
        row["note"] = info.get("title", "")[:50]
    if keep_dir:
        ext = os.path.splitext(unquote(name.split("''")[-1]).strip('"; ') if "''" in name else "")[1] or ".mp4"
        final = os.path.join(keep_dir, f"{label}{ext}")
        os.replace(path, final)
        row["note"] += f"  -> {final}"
    else:
        os.remove(path)
    return row


def read_links(path: str):
    items = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            label, _, link = line.partition(" ")
            if link.strip():
                items.append((label.strip(), link.strip()))
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("links", nargs="*", help="label=url pairs")
    ap.add_argument("--links-file", "--links", dest="links_file", help="text file: one 'label url' per line")
    ap.add_argument("--base", default="http://localhost:8000", help="where vidfetch is running")
    ap.add_argument("--key", default=os.environ.get("ACCESS_KEY", ""), help="the ACCESS_KEY (or set the env var)")
    ap.add_argument("--quality", default="low", choices=["low", "medium", "high", "best", "audio"],
                    help="quality to test (default low: fastest)")
    ap.add_argument("--keep", metavar="DIR", help="keep downloaded files in DIR so you can play them")
    args = ap.parse_args()

    items = []
    for pair in args.links:
        label, sep, link = pair.partition("=")
        items.append((label, link) if sep and link.startswith("http") else ("link", pair))
    if args.links_file:
        items += read_links(args.links_file)
    if not items:
        ap.error("give some links, or --links scripts/links.txt")
    if args.keep:
        os.makedirs(args.keep, exist_ok=True)

    base = args.base.rstrip("/")
    try:
        with request(base + "/api/health", timeout=15) as res:
            health = json.loads(res.read())
    except Exception as exc:
        print(f"Can't reach {base}: {error_text(exc)}\nIs the app running?", file=sys.stderr)
        return 2
    if not health.get("configured"):
        print("The app has no ACCESS_KEY set (or VIDFETCH_OPEN=1).", file=sys.stderr)
        return 2

    print(f"Testing {len(items)} link(s) against {base} at quality '{args.quality}'\n")
    rows = []
    for label, link in items:
        print(f"... {label}", flush=True)
        rows.append(check(label, link, base, args.key, args.quality, args.keep))

    print("\n{:<16} {:<5} {:>8} {:>9} {:>7}  {}".format("platform", "", "lookup", "download", "MB", "note"))
    for r in rows:
        f = lambda v, fmt: "-" if v is None else format(v, fmt)
        print("{:<16} {:<5} {:>8} {:>9} {:>7}  {}".format(
            r["platform"][:16], "PASS" if r["ok"] else "FAIL",
            f(r["lookup"], ".1f") + "s" if r["lookup"] is not None else "-",
            f(r["download"], ".1f") + "s" if r["download"] is not None else "-",
            f(r["mb"], ".1f"), r["note"]))
    passed = sum(r["ok"] for r in rows)
    print(f"\n{passed}/{len(rows)} passed")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
