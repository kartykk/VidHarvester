"""
Lightweight CLI parser tests (no network).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import main as vid_main


def test_cli_parser_crawl():
    parser = vid_main.build_parser()
    args = parser.parse_args([
        "crawl", "--url", "https://test.com",
        "--depth", "2", "--max-pages", "50", "--output", "out.json"
    ])
    assert args.url == "https://test.com"
    assert args.depth == 2
    assert args.max_pages == 50


def test_cli_parser_download():
    parser = vid_main.build_parser()
    args = parser.parse_args([
        "download", "--input", "videos.json", "--all",
        "--output-dir", "./dl", "--no-ytdlp"
    ])
    assert args.input == "videos.json"
    assert args.all is True
    assert args.no_ytdlp is True
