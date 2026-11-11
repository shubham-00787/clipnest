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


def probe(path: str) -> dict | None:
    """Ask ffmpeg what's inside the file: resolution and whether there is sound.
    Returns None when no ffmpeg is available."""
    import re
    import shutil
    import subprocess

    exe = shutil.which("ffmpeg")
    if not exe:
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    out = subprocess.run([exe, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    size = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    return {"audio": "Audio:" in out, "video": "Video:" in out,
            "res": f"{size.group(1)}x{size.group(2)}" if size else None}


def check(label: str, link: str, base: str, key: str, quality: str, keep_dir: str | None) -> dict:
    row = {"platform": label, "ok": False, "status": "FAIL", "lookup": None, "download": None, "mb": None, "note": ""}
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
        facts = probe(path)
        if facts is not None and quality != "audio" and not facts["audio"] and info.get("no_sound"):
            row["ok"] = True
            row["status"] = "PASS"
            row["note"] = f"[{facts['res']}] silent, and the site says the original has no sound: " + info.get("title", "")[:30]
        elif facts is not None and quality != "audio" and not facts["audio"]:
            # Could be the app, or a source video that really is silent: flag it, don't hide it.
            row["status"] = "WARN"
            row["note"] = f"file has NO SOUND ({facts['res'] or 'unknown size'}); check the original has sound"
        else:
            row["ok"] = True
            row["status"] = "PASS"
            res = f"[{facts['res']}] " if facts and facts.get("res") else ""
            row["note"] = res + info.get("title", "")[:45]
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


# platform label -> yt-dlp extractor key
YTDLP_SAMPLES = [
    ("youtube", "Youtube", None),
    ("youtube-shorts", "Youtube", "/shorts/"),
    ("instagram", "Instagram", None),
    ("facebook", "Facebook", None),
    ("facebook-reel", "FacebookReel", None),
    ("snapchat", "SnapchatSpotlight", None),
    ("tiktok", "TikTok", None),
    ("x", "Twitter", "/status/"),
    ("reddit", "Reddit", None),
]


_KNOWN_BROKEN = ("cannot parse", "unable to extract", "requires login", "login required")


def ytdlp_sample_links(per_platform: int = 3):
    """Up to `per_platform` example links per platform, from the test cases
    yt-dlp ships with its extractors. Cases yt-dlp itself marks as skipped or
    expected to warn about parse problems are left out, and the smoke test
    tries the next example when one fails, so one dead sample doesn't fail a
    whole platform."""
    from yt_dlp.extractor import gen_extractor_classes

    by_key = {ie.ie_key(): ie for ie in gen_extractor_classes()}
    picked = []
    for label, key, must_contain in YTDLP_SAMPLES:
        ie = by_key.get(key)
        if ie is None:
            print(f"(yt-dlp has no {key} extractor; skipping {label})", file=sys.stderr)
            continue
        if hasattr(ie, "get_testcases"):
            cases = list(ie.get_testcases(include_onlymatching=(label == "youtube-shorts")))
        else:
            cases = list(getattr(ie, "_TESTS", [])) + ([ie._TEST] if getattr(ie, "_TEST", None) else [])
        urls = []
        for case in cases:
            url = case.get("url", "")
            warnings = " ".join(case.get("expected_warnings") or []).lower()
            if case.get("skip") or not url.startswith("http") or any(w in warnings for w in _KNOWN_BROKEN):
                continue
            if must_contain and must_contain not in url:
                continue
            if not must_contain and case.get("only_matching"):
                continue
            urls.append(url)
            if len(urls) == per_platform:
                break
        if urls:
            picked.append((label, urls))
        else:
            print(f"(no usable example for {label})", file=sys.stderr)
    return picked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("links", nargs="*", help="label=url pairs")
    ap.add_argument("--links-file", "--links", dest="links_file", help="text file: one 'label url' per line")
    ap.add_argument("--base", default="http://localhost:8000", help="where vidfetch is running")
    ap.add_argument("--key", default=os.environ.get("ACCESS_KEY", ""), help="the ACCESS_KEY (or set the env var)")
    ap.add_argument("--quality", default="low", choices=["low", "medium", "high", "best", "audio"],
                    help="quality to test (default low: fastest)")
    ap.add_argument("--keep", metavar="DIR", help="keep downloaded files in DIR so you can play them")
    ap.add_argument("--ytdlp-samples", action="store_true",
                    help="also test one example link per platform taken from yt-dlp's own test cases")
    args = ap.parse_args()

    items = []
    for pair in args.links:
        label, sep, link = pair.partition("=")
        items.append((label, [link]) if sep and link.startswith("http") else ("link", [pair]))
    if args.links_file:
        items += [(label, [link]) for label, link in read_links(args.links_file)]
    if args.ytdlp_samples:
        try:
            items += ytdlp_sample_links()
        except ImportError:
            ap.error("--ytdlp-samples needs yt-dlp installed in this Python")
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

    print(f"Testing {len(items)} platform(s) against {base} at quality '{args.quality}'\n")
    rank = {"PASS": 2, "WARN": 1, "FAIL": 0}
    rows = []
    for label, urls in items:
        best = None
        for n, link in enumerate(urls, 1):
            print(f"... {label} ({n}/{len(urls)})", flush=True)
            row = check(label, link, base, args.key, args.quality, args.keep)
            if best is None or rank[row["status"]] > rank[best["status"]]:
                best = row
            if row["status"] == "PASS":
                break
        if len(urls) > 1 and best["status"] != "PASS":
            best["note"] += f"  (all {len(urls)} examples tried)"
        rows.append(best)

    print("\n{:<16} {:<5} {:>8} {:>9} {:>7}  {}".format("platform", "", "lookup", "download", "MB", "note"))
    for r in rows:
        f = lambda v, fmt: "-" if v is None else format(v, fmt)
        print("{:<16} {:<5} {:>8} {:>9} {:>7}  {}".format(
            r["platform"][:16], r["status"],
            f(r["lookup"], ".1f") + "s" if r["lookup"] is not None else "-",
            f(r["download"], ".1f") + "s" if r["download"] is not None else "-",
            f(r["mb"], ".1f"), r["note"]))
    count = {s: sum(r["status"] == s for r in rows) for s in rank}
    print(f"\n{count['PASS']} passed, {count['WARN']} warnings, {count['FAIL']} failed")
    return 1 if count["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
