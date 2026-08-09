"""
VidHarvester - Shared utilities
"""
import os
import re
import json
import time
import random
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urljoin, parse_qs, urlencode, urlunparse
from typing import Set, Optional, Dict, Any, List

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore


# Common tracking / noise parameters to strip for better dedup
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "_ga", "ref", "source",
    "campaign", "mc_cid", "mc_eid", "igshid", "si", "feature", "kw"
}

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".ts", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg"}
HLS_EXTENSIONS = {".m3u8", ".m3u"}
DASH_EXTENSIONS = {".mpd"}
ALL_VIDEO_EXTS = VIDEO_EXTENSIONS | HLS_EXTENSIONS | DASH_EXTENSIONS

# Reasonable User-Agent
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def setup_logging(log_file: Optional[Path] = None, level: int = logging.INFO) -> logging.Logger:
    """Setup logger that writes to both console and optional file.
    Safe to call multiple times — avoids duplicate handlers.
    """
    logger = logging.getLogger("vidharvester")
    logger.setLevel(level)

    # Only add handlers if none exist yet (prevents duplicates on re-init)
    if not logger.handlers:
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S"
        )

        # Console
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)

        # File (optional)
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)

    return logger


def normalize_url(url: str) -> str:
    """Normalize URL for deduplication: lower scheme/host, remove tracking params, sort query."""
    if not url:
        return url
    lower_url = url.lower()
    if not lower_url.startswith(("http://", "https://")):
        return url

    try:
        p = urlparse(url)

        # Lowercase scheme and netloc
        scheme = p.scheme.lower()
        netloc = p.netloc.lower()

        # Clean query params
        if p.query:
            qs = parse_qs(p.query, keep_blank_values=True)
            cleaned = {}
            for k, v in qs.items():
                if k.lower() not in TRACKING_PARAMS:
                    cleaned[k] = v
            # Rebuild query, sorted keys for stability
            new_query = urlencode(cleaned, doseq=True) if cleaned else ""
        else:
            new_query = ""

        # Remove default ports
        if (scheme == "http" and netloc.endswith(":80")) or (scheme == "https" and netloc.endswith(":443")):
            netloc = netloc.rsplit(":", 1)[0]

        # Reconstruct (path is kept as-is, fragment dropped)
        normalized = urlunparse((scheme, netloc, p.path, p.params, new_query, ""))
        return normalized
    except Exception:
        return url


def is_same_domain(url: str, base_domain: str) -> bool:
    """Return True if url belongs to base_domain (or subdomain).
    Handles IP addresses and localhost specially.
    """
    try:
        netloc = urlparse(url).netloc.lower().split(":")[0]
        base = base_domain.lower().split(":")[0]

        # IP or localhost special case
        if netloc in ("localhost", "127.0.0.1", "::1") or base in ("localhost", "127.0.0.1", "::1"):
            return netloc == base or netloc in ("localhost", "127.0.0.1") and base in ("localhost", "127.0.0.1")

        return netloc == base or netloc.endswith("." + base) or base.endswith("." + netloc)
    except Exception:
        return False


def get_base_domain(url: str) -> str:
    """Return a reasonable base domain for same-domain crawling.
    Handles IPs, localhost, and common multi-part TLDs.
    """
    try:
        netloc = urlparse(url).netloc.lower().split(":")[0]
        if netloc in ("localhost", "127.0.0.1", "::1"):
            return netloc
        parts = netloc.split(".")
        if len(parts) >= 3:
            if ".".join(parts[-2:]) in {"co.uk", "com.au", "co.jp", "com.br", "co.nz", "co.in"}:
                return ".".join(parts[-3:])
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return netloc
    except Exception:
        return url or ""


def make_absolute(base_url: str, link: str) -> Optional[str]:
    """Resolve relative link against base."""
    if not link:
        return None
    link = link.strip()
    if link.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    try:
        return urljoin(base_url, link)
    except Exception:
        return None


def is_video_url(url: str) -> bool:
    """Quick check if URL looks like a direct video or streaming manifest.
    Tries to avoid obvious false positives (tracking pixels, ad scripts, etc.).
    """
    if not url or len(url) < 10:
        return False
    lower = url.lower()

    # Quick rejection of obvious non-video junk
    bad_substrings = ("pixel", "tracker", "analytics", "beacon", "ads.", "/ad/", "doubleclick", "googletag")
    if any(b in lower for b in bad_substrings):
        return False

    path = urlparse(lower).path
    for ext in ALL_VIDEO_EXTS:
        if path.endswith(ext):
            return True

    # Query param patterns
    if any(x in lower for x in (".mp4?", ".webm?", ".m3u8?", ".mpd?")):
        return True

    # Some CDNs put the extension in the query (rare but happens)
    if any(f"ext={e[1:]}" in lower for e in ALL_VIDEO_EXTS):
        return True

    return False


def classify_video_type(url: str) -> str:
    """Classify discovered video URL type for yt-dlp strategy."""
    lower = url.lower()
    path = urlparse(lower).path
    if any(path.endswith(ext) for ext in HLS_EXTENSIONS) or ".m3u8" in lower:
        return "hls"
    if any(path.endswith(ext) for ext in DASH_EXTENSIONS) or ".mpd" in lower:
        return "dash"
    # YouTube / Vimeo / known sites that yt-dlp excels at
    if any(host in lower for host in ("youtube.com", "youtu.be", "vimeo.com", "dailymotion", "twitch.tv", "tiktok.com")):
        return "yt-dlp"
    if is_video_url(url):
        return "direct"
    # Default to yt-dlp for unknown embeds (it handles 1000+ sites)
    return "yt-dlp"


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Make a safe filename. Removes filesystem-illegal + some other noisy characters."""
    if not name:
        return "video"
    # Remove illegal + common noisy characters
    name = re.sub(r'[\\/:*?"<>|!@#$%^&*+=`~]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > max_len:
        name = name[:max_len].rsplit(" ", 1)[0] + "..."
    return name or "video"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def polite_sleep(min_sec: float = 0.8, max_sec: float = 2.2) -> None:
    """Random delay to be nice to servers."""
    time.sleep(random.uniform(min_sec, max_sec))


# ---------------- Proxy & Session helpers (robustness) ----------------

def get_proxy_dict(proxy: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Return a requests-compatible proxies dict from explicit proxy or env vars."""
    if proxy:
        proxy = proxy.strip()
        if not proxy:
            return None
        # Support "http://user:pass@host:port" or just "host:port"
        if not proxy.startswith(("http://", "https://", "socks5://")):
            proxy = "http://" + proxy
        return {"http": proxy, "https": proxy}

    # Fallback to environment
    http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if http_proxy or https_proxy:
        proxies = {}
        if http_proxy:
            proxies["http"] = http_proxy
        if https_proxy:
            proxies["https"] = https_proxy
        return proxies or None
    return None


def create_robust_session(
    user_agent: str = DEFAULT_USER_AGENT,
    proxy: Optional[str] = None,
    timeout: int = 25,
) -> requests.Session:
    """Create a requests.Session with good defaults for robustness."""
    import requests
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    })
    proxies = get_proxy_dict(proxy)
    if proxies:
        session.proxies.update(proxies)
    # Reasonable adapter with retries for transient errors
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# (proxy helpers use os and requests already imported at top)


def load_video_list(path: Path) -> List[Dict[str, Any]]:
    """Load existing video list JSON. Returns [] on error/missing."""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "videos" in data:
            return data["videos"]
        return []
    except Exception:
        return []


def save_video_list(path: Path, videos: List[Dict[str, Any]], meta: Optional[Dict[str, Any]] = None) -> None:
    """Save video list. Creates parent dirs."""
    ensure_dir(path.parent)
    payload = {
        "meta": meta or {"saved_at": now_iso()},
        "videos": videos
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def export_urls_txt(path: Path, videos: List[Dict[str, Any]]) -> Path:
    """Export just the video URLs (one per line)."""
    txt_path = path.with_suffix(".txt") if path.suffix else path.parent / (path.name + "_urls.txt")
    ensure_dir(txt_path.parent)
    with open(txt_path, "w", encoding="utf-8") as f:
        for v in videos:
            if v.get("url"):
                f.write(v["url"] + "\n")
    return txt_path


def export_csv(path: Path, videos: List[Dict[str, Any]]) -> Path:
    """Simple CSV export."""
    import csv
    csv_path = path.with_suffix(".csv") if path.suffix else path.parent / (path.name + ".csv")
    ensure_dir(csv_path.parent)
    keys = ["url", "source_page", "discovered_at", "status", "type"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for v in videos:
            row = {k: v.get(k, "") for k in keys}
            w.writerow(row)
    return csv_path


def merge_video_lists(existing: List[Dict[str, Any]], new_videos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge new videos into existing by normalized URL. Preserve status of existing entries."""
    url_to_entry: Dict[str, Dict[str, Any]] = {}
    for v in existing:
        norm = normalize_url(v.get("url", ""))
        if norm:
            url_to_entry[norm] = v

    for nv in new_videos:
        norm = normalize_url(nv.get("url", ""))
        if not norm:
            continue
        if norm not in url_to_entry:
            url_to_entry[norm] = nv
        else:
            # Keep existing status etc, but can update source_page if wanted
            existing_entry = url_to_entry[norm]
            # Only upgrade source if current is unknown or something; keep simple
            if not existing_entry.get("source_page"):
                existing_entry["source_page"] = nv.get("source_page", "")
    return list(url_to_entry.values())


def filter_videos(videos: List[Dict[str, Any]],
                  status: Optional[str] = None,
                  vtype: Optional[str] = None,
                  keyword: Optional[str] = None) -> List[Dict[str, Any]]:
    """Filter video list."""
    result = videos
    if status:
        result = [v for v in result if v.get("status") == status]
    if vtype:
        result = [v for v in result if v.get("type") == vtype]
    if keyword:
        kw = keyword.lower()
        result = [v for v in result
                  if kw in (v.get("url", "") + " " + v.get("source_page", "")).lower()]
    return result


def extract_title_from_html(html: str) -> Optional[str]:
    """Best-effort extraction of a human-readable title from HTML."""
    if not html or BeautifulSoup is None:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")

        # 1. Open Graph title (often the best)
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            return og["content"].strip()

        # 2. Twitter title
        tw = soup.find("meta", attrs={"name": "twitter:title"})
        if tw and tw.get("content"):
            return tw["content"].strip()

        # 3. Standard <title>
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
            # Clean common suffixes
            for sep in [" - ", " | ", " – ", " — "]:
                if sep in title:
                    title = title.split(sep)[0].strip()
                    break
            return title

        # 4. h1 fallback
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            return h1.get_text(strip=True)[:120]

    except Exception:
        pass
    return None
