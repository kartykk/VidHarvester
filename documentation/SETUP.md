# Setup — VidHarvester

## Prerequisites

- macOS host with Volume A mounted
- Tooling for: python

## Commands

- `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`

## Environment

- Use `.env` if present; document keys only (no values in git).


## From project README

```bash
cd /path/to/VidHarvester

# Create virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

pip install -r requirements.txt
```

```bash
pip install playwright
playwright install   # downloads Chromium (~150-200 MB)
```

```bash
python main.py crawl \
  --url "https://example.com/videos" \
  --depth 3 \
  --max-pages 200 \
  --output videos.json
```

```bash
python main.py crawl --url "https://example.com/videos" --load videos.json --output videos.json
```

```bash
python main.py download --input videos.json --output-dir ./downloads
```
