<div align="center">
  <img src="assets/vidharvester_logo.png" alt="VidHarvester Logo" width="320" />

  # VidHarvester
  ### Video link harvester & downloader
</div>

VidHarvester is a Python tool that:

1. Crawls a website (BFS) and discovers **video links**: direct `.mp4`, HLS `.m3u8`, DASH, embeds, and JavaScript-loaded sources.
2. Builds a clean, deduplicated list (`videos.json`).
3. Downloads everything, or only the videos you select, using **yt-dlp** as the primary engine with a direct `requests` fallback.

It works in two phases (**Discovery**, then **Download**) and can be used from the CLI or a Streamlit GUI.

**Tech stack:** Python 3.10+, requests, BeautifulSoup/lxml, yt-dlp, tqdm, rich, Streamlit + pandas (GUI), and optional Playwright (browser mode).

---

## Features

- **Robust crawling**: BFS + depth + max pages limit + same-domain protection
- **Deep video discovery**:
  - BeautifulSoup + extensive regex over HTML + inline JavaScript
  - `<video>`, `<source>`, `data-*`, `href`, iframes
  - **Open-source Playwright browser mode** (fully controlled via code):
  - Real **network response interception** for JS-loaded videos
  - **Advanced Cloudflare / Turnstile solver** (greatly reduced need for manual clicks):
    - Persistent Playwright browser profile (cookies + solved sessions are reused across runs)
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
git clone https://github.com/kartykk/VidHarvester.git
cd VidHarvester

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
```

### Using the GUI (recommended for most users)

```bash
streamlit run gui.py
```

- Enter starting URL + tweak depth / max pages / browser mode in the sidebar
- Click **Start / Continue Crawl**
- Review the table (checkboxes work for selection)
- Click **Download Selected Pending** or **Download ALL Pending**
- Use filters, mark/reset status, and export buttons

The GUI persists state to `gui_videos.json` by default (changeable in sidebar).

---

## Command Reference

```
python main.py crawl --help
python main.py download --help
python main.py export --help
python main.py stats --input videos.json
```

Key options:

| Command   | Important Flags |
|-----------|-----------------|
| `crawl`   | `--depth`, `--max-pages`, `--browser`, `--headed` (watch browser solve CF live), `--cf-manual`, `--proxy http://127.0.0.1:8080`, `--user-agent "..."`, `--load`, `--cross-domain` |
| `download`| `--all`, `--proxy`, `--cookies-from-browser chrome`, `--dry-run`, `--no-ytdlp` |
| `export`  | `--format txt\|csv\|json`, `--status`, `--keyword` |

**Robustness features**
- Proxy support (CLI + GUI + env vars)
- Playwright now performs **network-level media capture** (catches videos loaded via JavaScript/XHR)
- Per-domain polite delays + HTTP retries with backoff
- Direct downloads support resume (`Range` requests) and proper extension detection
- Post-download file validation (size + magic bytes)
- Custom User-Agent support
- Real integration tests with local HTTP server

---

## File Formats

The JSON list (`videos.json`) looks like this:

```json
{
  "meta": { ... },
  "videos": [
    {
      "url": "https://cdn.example.com/video/clip.mp4",
      "source_page": "https://example.com/videos?page=2",
      "discovered_at": "2026-04-12T19:03:11Z",
      "status": "pending",
      "type": "direct"
    },
    {
      "url": "https://www.youtube.com/watch?v=abc123",
      "source_page": "https://example.com/embed-test",
      "discovered_at": "2026-04-12T19:03:14Z",
      "status": "downloaded",
      "type": "yt-dlp"
    }
  ]
}
```

`status` is automatically updated during downloads.

---

## Ethical & Legal Notice

> **Only use VidHarvester on websites you own or have explicit permission to scrape.**
>
> Always respect `robots.txt`, the site's Terms of Service, and applicable copyright laws.
> Many sites prohibit automated scraping or downloading of their content.
>
> The authors of this tool are not responsible for any misuse.

Be a good citizen on the internet: use reasonable delays (already built-in), don't hammer servers, and only take what you have the right to take.

---

## Tips & Limitations

- **YouTube / Vimeo / big platforms**: yt-dlp is excellent. Use `--cookies-from-browser chrome` if you need private or age-restricted content.
- **HLS / m3u8**: yt-dlp usually gives the best results (it can merge segments).
- **Very heavy JS sites**: Enable Playwright mode. It is slower and uses more RAM.
- **Deduplication**: URLs are normalized (tracking params removed). You may still see near-duplicates with different tokens.
- **Large crawls**: Use `--max-pages` and start with small depth. Monitor the log.
- **Resume**: The tool skips files that already exist on disk (size > 1KB) and only processes `pending` items by default.
- **Failed downloads**: Use `python main.py download --input videos.json --status failed` or the GUI "mark as pending" buttons to retry.

---

## Project Structure

```
VidHarvester/
├── main.py           # CLI (crawl / download / export / stats)
├── crawler.py        # BFS + multi-strategy video extraction
├── downloader.py     # yt-dlp + direct requests downloader
├── gui.py            # Streamlit interface
├── utils.py          # URL normalization, logging, file helpers, merging
├── tests/            # pytest: CLI, utils, integration crawl (local HTTP server)
├── documentation/    # generated project notes
├── requirements.txt
└── LICENSE
```

---

## Development / Contributing

### Running tests

```bash
pip install pytest
python -m pytest tests/ -q
```

### Ideas for improvement

- Better video URL heuristics
- Support for more streaming protocols / subtitles
- Config file support
- Tor support

Keep the spirit: practical, reliable, and respectful of websites.

---

---

## Configuration

No `.env` is needed. Optional environment variables:

```env
HTTP_PROXY=
HTTPS_PROXY=
```

Known quirk: in browser mode, `crawler.py` stores the persistent Playwright profile in a **hard-coded, machine-specific absolute directory**. Change `profile_dir` in `crawler.py` before using `--browser` on another machine.

---

## Status

**Paused.**

## License

MIT. See [LICENSE](LICENSE). Follow the ethical notice above.
