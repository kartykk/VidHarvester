"""
Basic tests for VidHarvester utilities.
Run with: python -m pytest tests/ -q   (or just python -m pytest)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import (
    normalize_url, is_video_url, classify_video_type,
    merge_video_lists, filter_videos, get_base_domain, sanitize_filename
)


def test_normalize_url_strips_tracking():
    url = "https://example.com/video.mp4?utm_source=foo&fbclid=bar&token=keep"
    norm = normalize_url(url)
    assert "utm_source" not in norm
    assert "fbclid" not in norm
    assert "token=keep" in norm
    assert norm.startswith("https://example.com/video.mp4")


def test_normalize_url_lowercases_and_drops_fragment():
    url = "HTTPS://Example.COM/Path/Video.MP4?A=1#section"
    norm = normalize_url(url)
    assert norm.startswith("https://example.com/Path/Video.MP4")
    assert "A=1" in norm or "a=1" in norm   # query value case is preserved
    assert "#" not in norm


def test_is_video_url_detects_common():
    assert is_video_url("https://cdn.com/clip.mp4")
    assert is_video_url("https://site.com/hls/master.m3u8?token=x")
    assert is_video_url("https://example.com/video.webm?foo=bar")
    assert not is_video_url("https://example.com/page.html")
    assert not is_video_url("https://cdn.com/pixel.gif")
    assert not is_video_url("https://ads.example.com/tracker.mp4")  # bad substring rejection


def test_classify_video_type():
    assert classify_video_type("https://ex.com/a.m3u8") == "hls"
    assert classify_video_type("https://youtube.com/watch?v=abc123") == "yt-dlp"
    assert classify_video_type("https://vimeo.com/123456") == "yt-dlp"
    assert classify_video_type("https://cdn.com/real.mp4") == "direct"
    assert classify_video_type("https://unknown-site.com/embed/xyz") == "yt-dlp"


def test_merge_video_lists_dedups_and_preserves_status():
    existing = [
        {"url": "https://a.com/1.mp4", "status": "downloaded", "type": "direct", "source_page": "old"}
    ]
    new = [
        {"url": "https://a.com/1.mp4", "status": "pending", "type": "direct", "source_page": "new"},
        {"url": "https://a.com/2.mp4", "status": "pending", "type": "hls"}
    ]
    merged = merge_video_lists(existing, new)
    assert len(merged) == 2
    # existing status should be preserved
    one = next(m for m in merged if "1.mp4" in m["url"])
    assert one["status"] == "downloaded"
    assert one["source_page"] == "old"


def test_filter_videos():
    videos = [
        {"url": "https://a.com/clip.mp4", "status": "pending", "type": "direct"},
        {"url": "https://b.com/stream.m3u8", "status": "downloaded", "type": "hls"},
        {"url": "https://c.com/movie.mp4", "status": "pending", "type": "direct"},
    ]
    assert len(filter_videos(videos, status="pending")) == 2
    assert len(filter_videos(videos, vtype="hls")) == 1
    assert len(filter_videos(videos, keyword="stream")) == 1


def test_get_base_domain():
    assert get_base_domain("https://www.example.co.uk/page") == "example.co.uk"
    assert get_base_domain("https://sub.domain.com/video") == "domain.com"
    assert get_base_domain("https://example.com") == "example.com"


def test_sanitize_filename():
    # Special chars become underscores
    cleaned = sanitize_filename("My Cool Video!@#")
    assert cleaned.startswith("My Cool Video")
    assert "!" not in cleaned and "@" not in cleaned

    bad = 'file/name:with*bad?chars'
    cleaned = sanitize_filename(bad)
    assert "/" not in cleaned and ":" not in cleaned and "*" not in cleaned
    assert len(sanitize_filename("x" * 200)) <= 123
