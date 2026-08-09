#!/usr/bin/env python3
"""
VidHarvester - Video Link Harvester & Downloader
Main CLI entry point.

Usage examples:
    # Phase 1: Discover
    python main.py crawl --url "https://example.com/videos" --depth 3 --max-pages 150 --output videos.json

    # Continue / merge into existing list
    python main.py crawl --url "https://example.com/videos" --load videos.json --output videos.json

    # Phase 2: Download
    python main.py download --input videos.json --all --output-dir ./downloads

    # Download only pending + export urls for manual use
    python main.py download --input videos.json --output-dir ./downloads --export-urls

    # Filter + export
    python main.py export --input videos.json --status pending --format txt --output pending_videos.txt
"""
import argparse
import sys
from pathlib import Path
from typing import List, Dict, Any

from utils import (
    setup_logging, save_video_list, load_video_list, export_urls_txt,
    export_csv, filter_videos, merge_video_lists, now_iso
)
# Proxy and UA are passed through to crawler/downloader
from crawler import crawl_site, Crawler
from downloader import Downloader, download_from_list


def cmd_crawl(args: argparse.Namespace, logger):
    """Crawl a site and collect video links."""
    output_path = Path(args.output) if args.output else Path("videos.json")

    existing_videos: List[Dict[str, Any]] = []
    if args.load:
        load_path = Path(args.load)
        existing_videos = load_video_list(load_path)
        logger.info(f"Loaded {len(existing_videos)} existing videos from {load_path}")

    logger.info(f"Starting discovery from: {args.url}")

    new_videos = crawl_site(
        start_url=args.url,
        output_path=None,  # we handle saving + merging ourselves
        depth=args.depth,
        max_pages=args.max_pages,
        use_playwright=args.browser,
        same_domain=not args.cross_domain,
        allowed_domains=args.allowed_domain,
        logger=logger,
        proxy=getattr(args, "proxy", None),
        respect_robots=getattr(args, "respect_robots", True),
        cf_manual_help=getattr(args, "cf_manual", False),
        headed=getattr(args, "headed", False),
    )

    # Merge if we loaded something
    if existing_videos:
        final = merge_video_lists(existing_videos, new_videos)
        logger.info(f"Merged: {len(existing_videos)} existing + {len(new_videos)} new → {len(final)} total")
    else:
        final = new_videos

    meta = {
        "start_url": args.url,
        "crawled_at": now_iso(),
        "depth": args.depth,
        "max_pages": args.max_pages,
        "browser_mode": args.browser,
        "total_videos": len(final),
    }

    save_video_list(output_path, final, meta=meta)
    logger.info(f"Saved {len(final)} videos → {output_path}")

    # Also export a plain urls.txt for convenience
    txt = export_urls_txt(output_path, final)
    logger.info(f"Also wrote URL list: {txt}")


def cmd_download(args: argparse.Namespace, logger):
    """Download videos from a saved list."""
    input_path = Path(args.input)
    output_dir = Path(args.output_dir) if args.output_dir else Path("downloads")

    videos = load_video_list(input_path)
    if not videos:
        logger.error(f"No videos found in {input_path}")
        sys.exit(1)

    logger.info(f"Loaded {len(videos)} videos from {input_path}")

    # Optional filtering before download
    if args.status or args.type or args.keyword:
        before = len(videos)
        videos = filter_videos(videos, status=args.status, vtype=args.type, keyword=args.keyword)
        logger.info(f"Filtered: {before} → {len(videos)} videos")

    if args.dry_run:
        pending = [v for v in videos if v.get("status") == "pending"]
        logger.info(f"DRY RUN: would download {len(pending)} pending videos.")
        for v in pending[:20]:
            logger.info(f"  - {v['url']}")
        if len(pending) > 20:
            logger.info(f"  ... and {len(pending)-20} more")
        return

    dl = Downloader(
        videos=videos,
        output_root=output_dir,
        logger=logger,
        use_yt_dlp=not args.no_ytdlp,
        direct_fallback=not args.no_direct,
        cookies_from_browser=args.cookies_from_browser,
        proxy=getattr(args, "proxy", None),
        user_agent=getattr(args, "user_agent", None),
    )

    selected = None
    if args.urls:
        selected = args.urls.split(",") if "," in args.urls else [args.urls]

    stats = dl.download_all(
        only_pending=not args.all,
        selected_urls=selected,
    )

    # Persist updated statuses
    save_video_list(input_path, videos, meta={"last_download_run": now_iso()})

    # Optional exports after run
    if args.export_urls:
        p = export_urls_txt(input_path, videos)
        logger.info(f"Exported URLs: {p}")
    if args.export_csv:
        p = export_csv(input_path, videos)
        logger.info(f"Exported CSV: {p}")

    logger.info("Download session finished.")


def cmd_export(args: argparse.Namespace, logger):
    """Export filtered video list in different formats."""
    input_path = Path(args.input)
    videos = load_video_list(input_path)
    if not videos:
        logger.error("No videos to export.")
        return

    filtered = filter_videos(videos, status=args.status, vtype=args.type, keyword=args.keyword)
    logger.info(f"Exporting {len(filtered)} / {len(videos)} videos")

    out_path = Path(args.output) if args.output else input_path

    if args.format == "txt" or args.format == "urls":
        p = export_urls_txt(out_path, filtered)
    elif args.format == "csv":
        p = export_csv(out_path, filtered)
    elif args.format == "json":
        save_video_list(out_path, filtered)
        p = out_path
    else:
        logger.error(f"Unknown format: {args.format}")
        return

    logger.info(f"Exported to: {p}")


def cmd_stats(args: argparse.Namespace, logger):
    """Show quick stats about a video list."""
    videos = load_video_list(Path(args.input))
    if not videos:
        logger.info("Empty or unreadable list.")
        return

    total = len(videos)
    pending = sum(1 for v in videos if v.get("status") == "pending")
    downloaded = sum(1 for v in videos if v.get("status") == "downloaded")
    failed = sum(1 for v in videos if v.get("status") == "failed")

    types = {}
    for v in videos:
        t = v.get("type", "unknown")
        types[t] = types.get(t, 0) + 1

    logger.info("=== VidHarvester Stats ===")
    logger.info(f"Total videos: {total}")
    logger.info(f"  pending    : {pending}")
    logger.info(f"  downloaded : {downloaded}")
    logger.info(f"  failed     : {failed}")
    logger.info("By type:")
    for t, c in sorted(types.items()):
        logger.info(f"  {t:10s}: {c}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vidharvester",
        description="VidHarvester - Discover and download videos from websites",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="More verbose logging")

    sub = p.add_subparsers(dest="command", required=True)

    # --- CRAWL ---
    c = sub.add_parser("crawl", help="Crawl a site and discover video links")
    c.add_argument("--url", "-u", required=True, help="Starting URL to crawl")
    c.add_argument("--depth", type=int, default=3, help="Maximum crawl depth (default: 3)")
    c.add_argument("--max-pages", type=int, default=200, help="Hard limit on pages to visit")
    c.add_argument("--output", "-o", default="videos.json", help="Output JSON file (default: videos.json)")
    c.add_argument("--load", help="Load existing list and merge new discoveries (continue crawl)")
    c.add_argument("--browser", "--playwright", action="store_true",
                   help="Use open-source browser (Playwright) with advanced multi-stage human-simulation Cloudflare solver (realistic mouse, reading behavior, deliberate iframe checkbox clicks) + JS rendering + network capture. Use --headed to watch it try to solve automatically in real time.")
    c.add_argument("--cross-domain", action="store_true", help="Allow crawling other domains")
    c.add_argument("--allowed-domain", action="append",
                   help="Explicitly allow this domain (can be used multiple times)")
    c.add_argument("--proxy", help="Proxy to use for crawling (http://host:port or socks5://...)")
    c.add_argument("--user-agent", help="Custom User-Agent string")
    c.add_argument("--no-robots", "--ignore-robots", dest="respect_robots", action="store_false", default=True,
                   help="Ignore robots.txt (useful for some protected sites)")
    c.add_argument("--cf-manual", action="store_true",
                   help="Last-resort manual mode: if the advanced human-simulation solver still can't pass Cloudflare, pause and let you click the checkbox yourself")
    c.add_argument("--headed", action="store_true",
                   help="Run the Playwright browser in headed (visible) mode so you can literally watch it solve Cloudflare challenges in real time or manually click when needed.")
    c.set_defaults(func=cmd_crawl)

    # --- DOWNLOAD ---
    d = sub.add_parser("download", help="Download videos from a saved JSON list")
    d.add_argument("--input", "-i", required=True, help="Path to videos.json")
    d.add_argument("--all", action="store_true", help="Download all videos (ignore pending status)")
    d.add_argument("--output-dir", default="downloads", help="Root folder for downloads")
    d.add_argument("--no-ytdlp", action="store_true", help="Disable yt-dlp, use only direct downloader")
    d.add_argument("--no-direct", action="store_true", help="Disable direct fallback")
    d.add_argument("--cookies-from-browser", help="Import cookies from browser (chrome, firefox, safari, ...)")
    d.add_argument("--proxy", help="Proxy for downloads (http://host:port)")
    d.add_argument("--user-agent", help="Custom User-Agent for direct downloads")
    d.add_argument("--export-urls", action="store_true", help="Also write a urls.txt after download")
    d.add_argument("--export-csv", action="store_true", help="Also write a CSV after download")
    d.add_argument("--urls", help="Comma-separated list of specific URLs to download")
    d.add_argument("--status", help="Only download videos with this status (pending/downloaded/failed)")
    d.add_argument("--type", help="Only videos of this type (direct/hls/yt-dlp/...)")
    d.add_argument("--keyword", help="Only videos whose URL or source contains this text")
    d.add_argument("--dry-run", action="store_true", help="Show what would be downloaded, don't actually download")
    d.set_defaults(func=cmd_download)

    # --- EXPORT ---
    e = sub.add_parser("export", help="Export filtered video list")
    e.add_argument("--input", "-i", required=True)
    e.add_argument("--format", "-f", choices=["txt", "urls", "csv", "json"], default="txt")
    e.add_argument("--output", "-o")
    e.add_argument("--status")
    e.add_argument("--type")
    e.add_argument("--keyword")
    e.set_defaults(func=cmd_export)

    # --- STATS ---
    s = sub.add_parser("stats", help="Show statistics for a video list")
    s.add_argument("--input", "-i", required=True)
    s.set_defaults(func=cmd_stats)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    log_level = 10 if getattr(args, "verbose", False) else 20  # DEBUG / INFO
    logger = setup_logging(log_file=Path("vidharvester.log"), level=log_level)

    try:
        args.func(args, logger)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
