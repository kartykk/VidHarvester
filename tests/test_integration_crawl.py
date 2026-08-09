"""
Integration-style tests using a real local HTTP server.

These tests exercise the actual Crawler against a live (local) server.
No external network required.
"""
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urljoin

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from crawler import crawl_site


class VideoSiteHandler(BaseHTTPRequestHandler):
    """A tiny site with a couple of pages containing video links."""

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = """
            <html><body>
                <a href="/page2">Next</a>
                <video src="/real/video1.mp4"></video>
                <source src="/assets/clip.webm" type="video/webm">
            </body></html>
            """
            self._send(200, "text/html", html)
        elif self.path == "/page2":
            html = """
            <html><body>
                <a href="https://example.com/ignored">External</a>
                <a href="/deep/video.m3u8">HLS</a>
                <script>var src = "https://cdn.test.com/stream/playlist.m3u8";</script>
            </body></html>
            """
            self._send(200, "text/html", html)
        elif self.path.endswith((".mp4", ".webm", ".m3u8")):
            # Fake video response (just headers + small body)
            self._send(200, "video/mp4" if self.path.endswith(".mp4") else "application/vnd.apple.mpegurl", b"FAKEVIDEO123456")
        else:
            self._send(404, "text/plain", "not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if isinstance(body, (bytes, bytearray)):
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            data = body.encode("utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def log_message(self, format, *args):
        # Silence server logs during tests
        pass


@pytest.fixture(scope="module")
def local_server():
    port = 18765
    server = HTTPServer(("127.0.0.1", port), VideoSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"

    # Wait until the server is actually responding
    for _ in range(40):
        try:
            import urllib.request
            urllib.request.urlopen(base + "/", timeout=0.3).read()
            break
        except Exception:
            time.sleep(0.06)
    else:
        server.shutdown()
        raise RuntimeError("Test HTTP server failed to start")

    yield base

    server.shutdown()
    thread.join(timeout=1.0)


def test_crawl_local_site_finds_videos(local_server):
    """Crawl a local test site and verify we discover the embedded video URLs."""
    videos = crawl_site(
        start_url=local_server,
        depth=2,
        max_pages=20,
        use_playwright=False,   # use fast requests path for CI friendliness
        same_domain=True,
        proxy=None,
    )

    urls = {v["url"] for v in videos}

    # We should have found the direct mp4 and webm from page 1
    assert any("video1.mp4" in u for u in urls), f"Expected video1.mp4 in {urls}"
    assert any("clip.webm" in u for u in urls), f"Expected clip.webm in {urls}"

    # From page2 we should have the m3u8 (both relative and in JS)
    assert any("video.m3u8" in u for u in urls), f"Expected video.m3u8 in {urls}"
    assert any("playlist.m3u8" in u for u in urls), f"Expected playlist.m3u8 (from JS) in {urls}"

    # Must not have followed external domain
    assert not any("example.com" in u for u in urls)

    # All should be marked pending
    assert all(v["status"] == "pending" for v in videos)


def test_crawl_respects_same_domain(local_server):
    """Even if depth is high, discovered videos should come from pages on the test server (or be filtered by same_domain logic)."""
    videos = crawl_site(
        start_url=local_server + "/page2",
        depth=3,
        max_pages=30,
        same_domain=True,
    )
    # All videos must have been discovered from a page whose source is on our test server
    for v in videos:
        src = v.get("source_page", "")
        assert local_server.split(":")[0] in src or "127.0.0.1" in src or "localhost" in src, \
            f"Video {v['url']} came from unexpected source_page {src}"
