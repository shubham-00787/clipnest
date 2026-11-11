# vidfetch

Paste a link from YouTube, Shorts, Instagram, Facebook and other sites, and get the video back as an MP4. Medium quality (480p) is the default; Low, High, Best and Audio-only are options.

Free and open source: [yt-dlp](https://github.com/yt-dlp/yt-dlp) does the downloading, [Starlette](https://www.starlette.io/) serves the app, and it runs on free hosting (Vercel, Render) or in Docker.

> Only download videos you have the right to download. Private posts can't be fetched, and only sites that yt-dlp has a dedicated extractor for are supported.

## How it works

1. You open the app through your private link, then paste a video link (a whole share message is fine, the link is picked out).
2. `POST /api/prepare` checks the access key and link, looks the video up, and returns a signed download link that lasts 10 minutes.
3. `GET /api/file?t=…` downloads the video into a temporary folder, streams it to you, and deletes it.

No database, no accounts, nothing stored.

## Which sites work

From live tests on a home connection in India (11 Nov 2026, yt-dlp 2026.10.26). Sites change often, so re-check with `bash scripts/local_check.sh`.

| Platform | Result | Notes |
|---|---|---|
| YouTube and Shorts | Works | With sound from a home connection. On cloud hosting YouTube blocks the server, so vidfetch uses backup servers, see "YouTube from a cloud server" below. |
| Facebook videos | Works | With sound (tested with a public CNN video). |
| Facebook Reels | Works | The Reel tested was silent at the source. |
| X | Works | With sound (posts from 2024 and 2025). |
| Instagram | Works | Public reels and posts. Both test reels were silent, and Instagram itself marks them as having no sound; the page now shows "no sound" when a site says so. Some posts need `COOKIES_TXT`. |
| Snapchat Spotlight | Fallback added | yt-dlp's Snapchat reader no longer understands Snapchat's page, so vidfetch reads the video link from the current page format itself (`app/snapchat.py`). Removed or private Snaps show a clear message. |
| TikTok | Blocked in India | Works only where TikTok is reachable. |
| Reddit | Needs a login | Add `COOKIES_TXT` from a spare account. |

## Check each platform for real

With the app running, copy `scripts/links.example.txt` to `scripts/links.txt`, put in real public links, and run:

```bash
python3 scripts/smoke.py --base http://localhost:8000 --key change-me --links scripts/links.txt --keep ./smoke-files
```

It runs each link through the same steps as the web page and prints PASS or FAIL per platform with timings, file sizes and the reason for any failure. `--keep` saves the videos so you can play them. It also works against your deployed site: `--base https://your-app.vercel.app`.

## Use it from your phone's Share button

On Android, open the private link in Chrome and choose **Install app** (or **Add to Home screen**). After that, **vidfetch** appears in the Share menu of Instagram, Facebook, YouTube and other apps: share a video to it and the lookup starts by itself. iPhone Safari doesn't support this, but "Add to Home Screen" still gives a one-tap icon (open the private link once inside the installed app to unlock it).

## Settings (environment variables)

| Name | Required | What it does |
|---|---|---|
| `ACCESS_KEY` | **Yes** | A secret that unlocks the app. Share the private link `https://your-app/#k=<ACCESS_KEY>` with family; the page remembers the key and hides it from the address bar. The app refuses to work without a key (set `VIDFETCH_OPEN=1` to deliberately run without one, e.g. on your own computer). |
| `SECRET_KEY` | No | Key for signing download links. Defaults to a key derived from the access key. |
| `MAX_DURATION_SECONDS` | No | Longest video allowed. Default 900 (15 min). |
| `MAX_FILESIZE_MB` | No | Largest file allowed. Default 200. |
| `MAX_CONCURRENT` | No | Downloads at once per server instance. Default 2. |
| `YTDLP_PROXY` | No | e.g. `socks5://127.0.0.1:1080`. Used for YouTube only (all sites if `PROXY_ALL=1`). |
| `COOKIES_TXT` | No | Contents of a Netscape-format `cookies.txt`, from a **spare** account, never your main one. Helps with YouTube and Instagram. |
| `DENO_PATH` | No | Path to a Deno binary. Normally not needed: the `deno` pip package provides one (YouTube needs a JavaScript runtime). |
| `COBALT_INSTANCES` | No | Backup servers for YouTube when it blocks this server (comma-separated). Defaults to a few community cobalt servers; `off` disables the backup. |
| `ALLOW_GENERIC` | No | `1` lets yt-dlp's generic extractor try any web page. Off by default so strangers can't use the server to fetch arbitrary URLs. |

## Deploy on Vercel (free)

1. Sign in at [vercel.com](https://vercel.com) with GitHub, click **Add New → Project**, and import this repo.
2. Under **Environment Variables**, add `ACCESS_KEY` (make one with `python3 -c "import secrets; print(secrets.token_urlsafe(16))"`).
3. Deploy. You get `https://<name>.vercel.app`.
4. Optional, to pick up new yt-dlp versions automatically: in the project's **Settings → Git → Deploy Hooks** create a hook, then add its URL as the repo secret `DEPLOY_HOOK_URL`. The `weekly-redeploy` workflow calls it every Monday.

Vercel's free plan is for personal, non-commercial use. Limits to know: 5 minutes per request, 10 GB a month of data from the server. Videos are never re-encoded, only joined, to stay inside the free CPU allowance.

## Deploy on Render (free, backup)

Create a new **Blueprint** from this repo (it reads `render.yaml`) and enter `ACCESS_KEY` when asked. The free plan has 512 MB of memory, sleeps after 15 minutes idle, and allows about 5 GB of data a month.

## Run it yourself

The quickest way, on a Mac or Linux machine with Python 3.10+:

```bash
bash scripts/local_check.sh
```

It installs everything into `.venv`, runs the tests, does a real download for each platform (YouTube, Shorts, Instagram, Facebook, Snapchat Spotlight, TikTok, X, Reddit) using example links from yt-dlp's own test cases, prints PASS/FAIL per platform, and then opens the app in your browser. 
Step by step instead:

```bash
docker build -t vidfetch .
docker run --rm -p 8000:8000 -e ACCESS_KEY=change-me vidfetch
# open http://localhost:8000/#k=change-me
```

Without Docker: `pip install -r requirements.txt` (this also brings Deno, which YouTube needs, and a copy of ffmpeg), then `ACCESS_KEY=change-me uvicorn app.main:app` and open `http://localhost:8000/#k=change-me` (or start it with `VIDFETCH_OPEN=1` instead of a key if only you can reach it).

Tests: `pip install pytest && pytest`.

## YouTube from a cloud server

YouTube refuses most requests from cloud servers ("Sign in to confirm you're not a bot"); from a home connection it works directly. When it blocks the server, vidfetch:

1. gets the title, channel and thumbnail from YouTube's public oEmbed endpoint, and
2. asks community-run [cobalt](https://github.com/imputnet/cobalt) servers for the file, trying each in turn (`app/cobalt.py`).

Things to know:

- **They're volunteer-run.** Any of them can go down or start requiring keys. If the backup stops working, look for working servers on community lists and set `COBALT_INSTANCES`. The official cobalt.tools servers aren't used; they're not meant for other projects.
- **Privacy.** For YouTube on a blocked server, the video link is sent to one of those servers. Set `COBALT_INSTANCES=off` if you'd rather not.
- **Other options.** `COOKIES_TXT` from a spare account or a `YTDLP_PROXY` can also help yt-dlp itself; residential proxies are reliable but paid.

## Project layout

```
app/core.py     settings, link checks, quality presets, signed tokens
app/ytdl.py     yt-dlp wrapper
app/main.py     the web app (Vercel runs this directly)
public/         the web page
tests/          pytest suite
```

## License

MIT
