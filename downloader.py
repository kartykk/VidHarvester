"""
VidHarvester - Downloader module

Primary engine: yt-dlp (best compatibility + quality)
Fallback: direct requests streaming for simple .mp4/.webm links

Features:
- Organized output: downloads/<domain>_<date>/
- Skip already downloaded (by file existence + size)
- Progress reporting (tqdm + yt-dlp hooks)
- Status updates in the video list (pending / downloaded / failed)
- Retry support for previously failed items
- Export of clean urls.txt for manual yt-dlp use
"""
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable
from urllib.parse import urlparse

import requests
from tqdm import tqdm

from utils import (
    normalize_url, ensure_dir, sanitize_filename, get_base_domain,
    polite_sleep, setup_logging, export_urls_txt, get_proxy_dict
)


class Downloader:
    def __init__(
        self,
        videos: List[Dict[str, Any]],
        output_root: Path = Path("downloads"),
        logger: Optional[Any] = None,
        use_yt_dlp: bool = True,
        direct_fallback: bool = True,
        max_retries: int = 2,
        cookies_from_browser: Optional[str] = None,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
    ):
        self.videos = videos
        self.output_root = ensure_dir(Path(output_root))
        self.logger = logger or setup_logging()
        self.use_yt_dlp = use_yt_dlp
        self.direct_fallback = direct_fallback
        self.max_retries = max_retries
        self.cookies_from_browser = cookies_from_browser
        self.proxy = proxy
        self.user_agent = user_agent or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

        # Current run folder (created on first download)
        self.run_dir: Optional[Path] = None

    def _prepare_run_dir(self, domain_hint: str) -> Path:
        if self.run_dir:
            return self.run_dir
        date_str = datetime.now().strftime("%Y%m%d_%H%M")
        safe_domain = re.sub(r"[^a-zA-Z0-9_.-]", "_", domain_hint)[:40]
        folder = self.output_root / f"{safe_domain}_{date_str}"
        self.run_dir = ensure_dir(folder)
        self.logger.info(f"Download folder: {self.run_dir}")
        return self.run_dir

    def _get_good_title(self, video: Dict[str, Any]) -> str:
        """Return the best available title for filename purposes."""
        for key in ("title", "video_title", "name"):
            t = video.get(key)
            if t and isinstance(t, str) and len(t.strip()) > 1:
                return sanitize_filename(t.strip())
        # Fallback to URL path
        try:
            path = urlparse(video.get("url", "")).path
            base = Path(path).stem
            if base and len(base) > 2:
                return sanitize_filename(base)
        except Exception:
            pass
        return "video"

    def _build_unique_filename(self, video: Dict[str, Any], ext: str) -> str:
        """Create a filename that uses the title but adds a short unique suffix
        to avoid collisions when multiple videos come from the same page.
        """
        title = self._get_good_title(video)
        # Short unique id from the URL (last 6 chars of hash is enough)
        url = video.get("url", "")
        short_id = str(abs(hash(url)) % 1000000)[:6].zfill(6)
        return f"{title}_{short_id}{ext}"

    # ---------------- yt-dlp path ----------------

    def _download_with_ytdlp(self, video: Dict[str, Any], target_dir: Path) -> bool:
        """Use yt-dlp Python API. Captures real title + final filename and stores it back into the video dict."""
        try:
            import yt_dlp
        except ImportError:
            self.logger.warning("yt-dlp not installed. Falling back to direct download.")
            return False

        url = video["url"]

        # If we have a good pre-discovered title, prefer it for the filename
        # so the on-disk name matches the title the user sees in the list.
        pre_title = self._get_good_title(video)
        if pre_title and pre_title not in ("video", "mov_bbb"):
            # Force a clean title-based template
            outtmpl = str(target_dir / f"{pre_title} [%(id)s].%(ext)s")
        else:
            outtmpl = str(target_dir / "%(title)s [%(id)s].%(ext)s")

        ydl_opts = {
            "outtmpl": outtmpl,
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 5,
            "consoletitle": False,
            "progress_hooks": [self._make_yt_progress_hook(url)],
        }

        if self.cookies_from_browser:
            ydl_opts["cookiesfrombrowser"] = (self.cookies_from_browser,)

        if self.proxy:
            ydl_opts["proxy"] = self.proxy

        if video.get("type") in ("hls", "dash"):
            ydl_opts["format"] = "best"

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

                # Extract useful metadata and persist it
                title = info.get("title") or info.get("fulltitle")
                final_filename = None
                if info.get("requested_downloads"):
                    final_filename = info["requested_downloads"][0].get("filepath")
                elif info.get("_filename"):
                    final_filename = info["_filename"]
                elif info.get("filename"):
                    final_filename = info["filename"]

                # Prefer yt-dlp title, but fall back to what we had from crawling
                final_title = title or self._get_good_title(video)
                video["title"] = final_title

                if final_filename:
                    video["saved_as"] = str(final_filename)
                    video["final_ext"] = Path(final_filename).suffix.lstrip(".")
                else:
                    # Fallback: construct what we think the filename was
                    ext = Path(final_filename).suffix if final_filename else ".mp4"
                    guessed = target_dir / f"{sanitize_filename(final_title)}{ext}"
                    video["saved_as"] = str(guessed)

                self.logger.info(f"  ✓ Downloaded (yt-dlp): {url[:90]}")
                return True

        except yt_dlp.utils.DownloadError as e:
            self.logger.warning(f"  yt-dlp failed for {url[:80]}: {str(e)[:140]}")
            return False
        except Exception as e:
            self.logger.warning(f"  yt-dlp unexpected error: {e}")
            return False

    def _make_yt_progress_hook(self, url: str) -> Callable:
        last_pct = [0]

        def hook(d):
            if d.get("status") == "downloading":
                pct = d.get("_percent_str", "").strip()
                if pct and pct != last_pct[0]:
                    last_pct[0] = pct
                    # Keep logs quiet; user sees tqdm elsewhere if needed
            elif d.get("status") == "finished":
                filename = d.get("filename", "")
                if filename:
                    self.logger.debug(f"    yt-dlp finished: {Path(filename).name}")
        return hook

    # ---------------- direct requests fallback ----------------

    def _guess_extension(self, url: str, content_type: str) -> str:
        """Best-effort extension from URL or Content-Type."""
        path = urlparse(url).path.lower()
        for ext in (".mp4", ".webm", ".mkv", ".mov", ".ts", ".m4v", ".avi"):
            if path.endswith(ext):
                return ext
        ct = (content_type or "").lower()
        if "webm" in ct:
            return ".webm"
        if "mp4" in ct or "mpeg" in ct:
            return ".mp4"
        if "quicktime" in ct:
            return ".mov"
        return ".mp4"  # safe default

    def _download_direct(self, video: Dict[str, Any], target_dir: Path) -> bool:
        """Direct streaming download with extension detection + basic resume support.
        The filename is now based on the video title (from page or discovery) so that
        the on-disk filename matches the title stored in the list/JSON.
        """
        url = video["url"]
        try:
            headers = {"User-Agent": self.user_agent}
            proxies = get_proxy_dict(self.proxy)

            # First HEAD to check size / support resume + get content type for extension
            head_resp = requests.head(url, timeout=15, allow_redirects=True,
                                      headers=headers, proxies=proxies)
            total_size = int(head_resp.headers.get("content-length", 0))
            accept_ranges = "bytes" in head_resp.headers.get("accept-ranges", "").lower()
            content_type = head_resp.headers.get("content-type", "")

            ext = self._guess_extension(url, content_type)
            filename = self._build_unique_filename(video, ext)
            target = target_dir / filename

            # Compute title early so we can use it in skip cases too
            title_base = self._get_good_title(video)

            # Skip if complete file already exists
            if target.exists():
                existing_size = target.stat().st_size
                if total_size > 0 and existing_size >= total_size:
                    self.logger.info(f"  ↷ Skip (complete): {target.name}")
                    video["saved_as"] = str(target)
                    video["title"] = title_base
                    return True
                elif total_size > 0 and accept_ranges and existing_size > 1024:
                    # Try to resume
                    self.logger.info(f"  ↻ Resuming direct download: {target.name} ({existing_size}/{total_size})")
                    resume_headers = dict(headers)
                    resume_headers["Range"] = f"bytes={existing_size}-"
                    resp = requests.get(url, stream=True, timeout=60, headers=resume_headers, proxies=proxies)
                    if resp.status_code in (206, 200):
                        mode = "ab"
                    else:
                        mode = "wb"  # server didn't honor range
                else:
                    mode = "wb"
            else:
                mode = "wb"

            if mode == "wb":
                resp = requests.get(url, stream=True, timeout=60, headers=headers, proxies=proxies)
                resp.raise_for_status()
                # Re-check size from this response
                total_size = int(resp.headers.get("content-length", total_size or 0))

            # Open in correct mode
            with open(target, mode) as f, tqdm(
                total=total_size or None,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=target.name[:55],
                leave=False,
                initial=target.stat().st_size if mode == "ab" else 0,
            ) as bar:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
                        if total_size:
                            bar.update(len(chunk))

            final_size = target.stat().st_size
            if final_size < 2048:
                target.unlink(missing_ok=True)
                self.logger.warning(f"  ✗ Too small, deleted: {url[:80]}")
                return False

            video["saved_as"] = str(target)
            video["final_ext"] = ext.lstrip(".")
            video["title"] = title_base   # Make title match the filename we chose
            self.logger.info(f"  ✓ Downloaded (direct): {target.name} ({final_size // 1024} KB)")
            return True

        except Exception as e:
            self.logger.warning(f"  Direct download failed: {e}")
            return False

    # ---------------- public API ----------------

    def _is_likely_valid_video(self, path: Path) -> bool:
        """Very lightweight sanity check (size + common magic bytes)."""
        try:
            if not path.exists() or path.stat().st_size < 4096:
                return False
            with open(path, "rb") as f:
                header = f.read(16)
            # Common video magic numbers (very rough but useful)
            if header.startswith(b"\x00\x00\x00\x1cftyp") or b"ftypmp4" in header or b"ftypisom" in header:  # MP4
                return True
            if header.startswith(b"\x1a\x45\xdf\xa3"):  # MKV / WEBM
                return True
            if b"WEBM" in header or b"matroska" in header:
                return True
            if header[4:8] == b"moov" or header[4:8] == b"mdat":  # quicktime-ish
                return True
            # If no magic match but size is reasonable, trust it (many streams)
            return path.stat().st_size > 50 * 1024
        except Exception:
            return False

    def download_one(self, video: Dict[str, Any]) -> bool:
        """Download a single video entry. Updates status + metadata in-place."""
        url = video.get("url")
        if not url:
            video["status"] = "failed"
            return False

        domain = get_base_domain(url)
        target_dir = self._prepare_run_dir(domain)

        success = False
        last_error = ""

        for attempt in range(self.max_retries + 1):
            try:
                if self.use_yt_dlp:
                    success = self._download_with_ytdlp(video, target_dir)
                    if success:
                        break
                    if not self.direct_fallback:
                        last_error = "yt-dlp failed and direct fallback disabled"
                        break
                    if video.get("type") == "direct":
                        success = self._download_direct(video, target_dir)
                        if success:
                            break

                elif self.direct_fallback:
                    success = self._download_direct(video, target_dir)
                    if success:
                        break
            except Exception as e:
                last_error = str(e)
                self.logger.warning(f"  Attempt {attempt+1} exception: {e}")

            if attempt < self.max_retries:
                backoff = 1.5 ** attempt
                self.logger.info(f"  Retry {attempt+1}/{self.max_retries} for {url[:70]} (backoff {backoff:.1f}s)")
                polite_sleep(backoff, backoff + 1.5)

        if success:
            # Post-download validation
            saved_path = video.get("saved_as")
            if saved_path:
                p = Path(saved_path)
                if not self._is_likely_valid_video(p):
                    self.logger.warning(f"  ⚠ Downloaded file may be invalid: {p.name}")
                    # We still mark as downloaded but leave a flag
                    video["warning"] = "size_or_magic_check_failed"

            video["status"] = "downloaded"
        else:
            video["status"] = "failed"
            video["last_error"] = last_error[:200] if last_error else "unknown"
            self.logger.error(f"  ✗ Failed after retries: {url[:90]}")

        return success

    def download_all(
        self,
        only_pending: bool = True,
        selected_urls: Optional[List[str]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, int]:
        """
        Download videos.

        Returns stats: {"downloaded": n, "skipped": n, "failed": n}
        """
        stats = {"downloaded": 0, "skipped": 0, "failed": 0}

        to_download = []
        for v in self.videos:
            if selected_urls is not None:
                if normalize_url(v.get("url", "")) not in {normalize_url(u) for u in selected_urls}:
                    continue
            if only_pending and v.get("status") != "pending":
                stats["skipped"] += 1
                continue
            to_download.append(v)

        total = len(to_download)
        if total == 0:
            self.logger.info("Nothing to download.")
            return stats

        self.logger.info(f"Starting download of {total} video(s)...")

        for idx, video in enumerate(to_download, 1):
            url = video.get("url", "")[:85]
            status = video.get("status", "pending")
            self.logger.info(f"[{idx}/{total}] {status.upper()}: {url}")

            if progress_callback:
                progress_callback(idx, total, video.get("url", ""))

            ok = self.download_one(video)
            if ok:
                stats["downloaded"] += 1
            else:
                stats["failed"] += 1

            # Be nice between videos
            if idx < total:
                polite_sleep(0.4, 1.1)

        self.logger.info(
            f"Download complete. Success: {stats['downloaded']} | "
            f"Failed: {stats['failed']} | Skipped: {stats['skipped']}"
        )
        return stats

    def get_pending_count(self) -> int:
        return sum(1 for v in self.videos if v.get("status") == "pending")

    def get_failed_count(self) -> int:
        return sum(1 for v in self.videos if v.get("status") == "failed")

    @staticmethod
    def load_videos(path: Path) -> List[Dict[str, Any]]:
        from utils import load_video_list
        return load_video_list(path)

    @staticmethod
    def save_videos(path: Path, videos: List[Dict[str, Any]], meta: Optional[Dict] = None):
        from utils import save_video_list
        save_video_list(path, videos, meta)


def download_from_list(
    input_path: Path,
    output_dir: Path = Path("downloads"),
    all_videos: bool = False,
    selected: Optional[List[str]] = None,
    logger: Optional[Any] = None,
    cookies_from_browser: Optional[str] = None,
) -> Dict[str, int]:
    """Convenience function used by CLI."""
    videos = Downloader.load_videos(input_path)
    if not videos:
        if logger:
            logger.error(f"No videos found in {input_path}")
        return {"downloaded": 0, "skipped": 0, "failed": 0}

    dl = Downloader(
        videos=videos,
        output_root=output_dir,
        logger=logger,
        cookies_from_browser=cookies_from_browser,
    )
    stats = dl.download_all(only_pending=not all_videos, selected_urls=selected)
    Downloader.save_videos(input_path, videos, meta={"last_download": datetime.utcnow().isoformat()})
    return stats
