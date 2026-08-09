- See central [INDEX.md](file:///Volumes/A/Documents/project-documentation/INDEX.md) for cross-links.

## Source README excerpt

# VidHarvester

**Video Link Harvester & Downloader**

A practical, production-ready Python tool that:

1. Crawls any website (BFS) and discovers **all video links** (direct `.mp4`, HLS `.m3u8`, DASH, embeds, JavaScript-loaded sources, etc.).
2. Builds a clean, deduplicated list.
3. Lets you download everything (or selected videos) reliably using **yt-dlp** as the primary engine + a direct `requests` fallback.

Works in two clear phases:

- **Discovery** → save `videos.json`
- **Download** → one command (or via nice Streamlit GUI)

---

## Features

- **Robust crawling**: BFS + depth + max pages limit + same-domain protection
- **Deep video discovery**:
  - BeautifulSoup + extensive regex over HTML + inline JavaScript
  - `<video>`, `<source>`, `data-*`, `href`, iframes
  - **Open-source Playwright browser mode** (fully controlled via code):
  - Real **network response interception** for JS-loaded videos
  - **Advanced Cloudflare / Turnstile solver** (greatly reduced need for manual clicks):
    - Persistent browser profile saved on Volume A (cookies + solved sessions are reused across runs)
    - Multi-stage human simulation:
      - "Reading the page" behavior (slow deliberate mouse scanning)
      - Natural curved mouse paths with realistic speed variation and micro-pauses
      - Deliberate hover + slow click on the actual checkbox inside the Cloudflare iframe
    - 4 progressive stages of increasing human-like activity before giving up
    - Automatic iframe detection and interaction
    - `--cf-manual` as last-resort fallback only (you rarely need it now)
  - `--headed` flag: watch the browser solve challenges live in real time
  - Stealth + persistent profile + behavioral simulation
  - Significantly higher success rate on heavily protected sites
- **Proxy support** everywhere: `--proxy`, GUI sidebar, requests, yt-dlp, and Playwright
- **Robust crawling**: per-domain rate limiting, connection retries, custom User-Agent, environment proxy fallback (HTTP_PROXY)
- **Polite & safe**: random delays, `robots.txt` respect, visited tracking
- **Deduplication** with URL normalization (strips tracking params)
- **Primary downloader**: [yt-dlp](https://github.com/yt-dlp/yt-dlp) (best quality, resumable, 1000+ sites including YouTube/Vimeo/etc.)
- **Direct fallback**: `requests` + `tqdm` for simple video files
- **Organized output**: `downloads/<domain>_<timestamp>/`
- **CLI + beautiful Streamlit GUI**
- Merge mode, filters, exports (JSON/CSV/TXT), status tracking (`pending` / `downloaded` / `failed`)
- Good logging to console + `vidharvester.log`

---

## Installation

```bash
cd /path/to/VidHarvester

# Create virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

pip install -r requirements.txt
```

### Optional: Playwright (for JS-heavy sites)

```bash
pip install playwright
playwright install   # downloads Chromium (~150-200 MB)
```

You only need this if you plan to use `--browser` / "Use headless browser" in the GUI.

---

## Quick Start

### Phase 1: Discover videos (CLI)

```bash
python main.py crawl \
  --url "https://example.com/videos" \
  --depth 3 \
  --max-pages 200 \
  --output videos.json
```

Continue / merge into an existing list:

```bash
python main.py crawl --url "https://example.com/videos" --load videos.json --output videos.json
```

### Phase 2: Download (CLI)

Download everything pending:

```bash
python main.py download --input videos.json --output-dir ./downloads
```

Download **all** regardless of status:

```bash
python main.py download --input videos.json --all
```

Only specific URLs or filtered:

```bash
python main.py download --input videos.json --urls "https://.../clip.mp4,https://.../video.m3u8"
```

Export clean URL list (great for using yt-dlp manually later):

```bash
python main.py export --input videos.json --format txt --output urls.txt
# then: yt-dlp -a urls.txt -o "downloads/%(title)s.%(ext)s"
``

## Related projects

See central INDEX.md.
