#!/usr/bin/env bash
# One command to set up, test and run vidfetch on your own computer:
#
#     bash scripts/local_check.sh
#
# Installs into ./.venv, runs the test suite, does a real download for each
# platform, then leaves the app running and opens it in your browser.
set -euo pipefail
cd "$(dirname "$0")/.."
KEY="${ACCESS_KEY:-local-test}"
PORT="${PORT:-8000}"
step() { printf '\n==> %s\n' "$1"; }

step "Finding Python 3.10 or newer"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then echo "Python 3.10+ is needed. On a Mac: brew install python@3.12"; exit 1; fi
echo "Using $("$PY" --version)"

step "Installing dependencies into .venv"
[ -d .venv ] || "$PY" -m venv .venv
. .venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt pytest

step "Checking helpers"
if command -v ffmpeg >/dev/null 2>&1; then echo "ffmpeg: $(command -v ffmpeg)"; else echo "ffmpeg: not installed, using the copy bundled with imageio-ffmpeg"; fi
if command -v deno >/dev/null 2>&1; then echo "deno:   $(command -v deno)"; else echo "deno:   $(python -c 'import deno; print(deno.find_deno_bin())' 2>/dev/null || echo 'MISSING: YouTube will fail')  (from the deno pip package)"; fi
echo "yt-dlp: $(python -m yt_dlp --version)"

step "Running the test suite"
python -m pytest -q

step "Starting the app on port $PORT"
ACCESS_KEY="$KEY" python -m uvicorn app.main:app --port "$PORT" --log-level warning > .server.log 2>&1 &
SERVER=$!
trap 'kill "$SERVER" 2>/dev/null || true' EXIT
for _ in $(seq 1 40); do
  curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break
  sleep 0.5
done

step "Real downloads, one per platform"
EXTRA=""
[ -f scripts/links.txt ] && EXTRA="--links scripts/links.txt"
set +e
# shellcheck disable=SC2086
python scripts/smoke.py --base "http://127.0.0.1:$PORT" --key "$KEY" --ytdlp-samples $EXTRA --keep smoke-files
set -e

step "The app is running"
echo "Try it:  http://localhost:$PORT/#k=$KEY"
echo "Test videos are saved in ./smoke-files. Server log: .server.log. Press Ctrl+C to stop."
open "http://localhost:$PORT/#k=$KEY" 2>/dev/null || true
wait "$SERVER"
