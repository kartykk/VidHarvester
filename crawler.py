"""
VidHarvester - Crawler module

BFS crawling + robust video link extraction.
Supports:
- BeautifulSoup + regex extraction from HTML and inline JS
- Multiple attribute sources (src, data-*, href, etc.)
- Optional Playwright headless rendering for JS-heavy sites
- Polite crawling + robots.txt respect (best effort)
- Same-domain by default + configurable allowed domains
"""
import os
import re
import json
import time
import random
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Set, List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, urljoin
from urllib import robotparser

import requests
from bs4 import BeautifulSoup

from utils import (
    normalize_url, is_same_domain, get_base_domain, make_absolute,
    is_video_url, classify_video_type, now_iso, polite_sleep,
    DEFAULT_USER_AGENT, ensure_dir, setup_logging,
    create_robust_session, get_proxy_dict, extract_title_from_html
)


# Regex patterns for video URLs (used on full page source including JS)
VIDEO_URL_RE = re.compile(
    r'["\']((?:https?:)?//[^"\']+?\.(?:mp4|webm|mkv|avi|mov|ts|m4v|flv|wmv|mpg|mpeg|m3u8|mpd)(?:[^"\']*?))["\']',
    re.IGNORECASE
)
# Broader pattern for source: or src: or file: assignments inside JS/JSON
SRC_LIKE_RE = re.compile(
    r'(?:source|src|file|url|video|stream)["\s:]+["\']((?:https?:)?//[^"\']+?\.(?:mp4|webm|m3u8|mpd)[^"\']*?)["\']',
    re.IGNORECASE
)
# Generic http(s) urls ending with common video indicators even without dot-ext in some cases
GENERIC_VIDEO_RE = re.compile(
    r'https?://[^\s"\'<>]+?(?:\.mp4|\.webm|\.m3u8|\.mpd|\.ts)(?:\?[^\s"\'<>]*)?',
    re.IGNORECASE
)


class Crawler:
    def __init__(
        self,
        start_url: str,
        depth: int = 3,
        max_pages: int = 200,
        delay_range: Tuple[float, float] = (0.7, 1.8),
        same_domain: bool = True,
        allowed_domains: Optional[List[str]] = None,
        use_playwright: bool = False,
        user_agent: str = DEFAULT_USER_AGENT,
        respect_robots: bool = True,
        logger: Optional[Any] = None,
        output_path: Optional[Path] = None,
        proxy: Optional[str] = None,
        cf_manual_help: bool = False,
        headed: bool = False,
    ):
        self.start_url = start_url.strip()
        self.depth = max(0, int(depth))
        self.max_pages = max(1, int(max_pages))
        self.delay_range = delay_range
        self.same_domain = same_domain
        self.allowed_domains = [d.lower() for d in (allowed_domains or [])]
        self.use_playwright = use_playwright
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self.logger = logger or setup_logging()
        self.output_path = output_path
        self.proxy = proxy

        # New: allow user to help with very hard Cloudflare challenges (user can click manually)
        self.cf_manual_help = cf_manual_help
        self.headed = headed

        # Robust session with proxy + retries
        self.session = create_robust_session(
            user_agent=self.user_agent,
            proxy=self.proxy,
        )

        self.visited: Set[str] = set()
        self.queue: deque = deque()  # (url, current_depth)
        self.videos: List[Dict[str, Any]] = []
        self.seen_video_urls: Set[str] = set()

        self.base_domain = get_base_domain(self.start_url)
        self.robots_allowed = {}  # cache netloc -> bool

        # Per-domain last access time for better politeness
        self._domain_last_access: Dict[str, float] = {}

        # Playwright page + network capture
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._captured_media: Set[str] = set()  # populated via network interception

        if self.use_playwright:
            self._init_playwright()

    def _init_playwright(self):
        try:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()

            # Use PERSISTENT context on Volume A so solved Cloudflare sessions (cookies) are reused.
            # This dramatically reduces future challenges on the same site.
            profile_dir = "/Volumes/A/VidHarvester/playwright_profile"
            os.makedirs(profile_dir, exist_ok=True)

            # Launch args for anti-detection
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-web-security",
            ]

            launch_persistent_kwargs = {
                "headless": not self.headed,
                "user_agent": self.user_agent,
                "viewport": {"width": 1366, "height": 768},
                "locale": "en-US",
                "timezone_id": "America/New_York",
                "extra_http_headers": {
                    "Accept-Language": "en-US,en;q=0.9",
                    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"macOS"',
                    "sec-fetch-site": "none",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-user": "?1",
                    "sec-fetch-dest": "document",
                },
                "permissions": ["geolocation"],
                "accept_downloads": False,
                "args": launch_args,
            }

            if self.proxy:
                p = self.proxy.strip()
                if not p.startswith(("http://", "https://", "socks5://")):
                    p = "http://" + p
                launch_persistent_kwargs["proxy"] = {"server": p}

            # Launch persistent context directly (correct API)
            context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                **launch_persistent_kwargs
            )
            self._context = context
            self._page = context.new_page()
            self._browser = context.browser  # for compatibility in close

            # Advanced stealth script (pure open-source, no external packages)
            self._page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });

                // Spoof canvas and webgl to look less automated
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Inc.';
                    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                    return getParameter.apply(this, arguments);
                };
            """)

            # === Real network media capture (this closes the gap in the original spec) ===
            self._captured_media.clear()

            def on_response(response):
                try:
                    url = response.url
                    headers = response.headers or {}
                    content_type = (headers.get("content-type") or "").lower()

                    is_media = (
                        is_video_url(url) or
                        any(x in content_type for x in ("video/", "mpegurl", "application/dash+xml", "application/vnd.apple.mpegurl")) or
                        any(ext in url.lower() for ext in (".m3u8", ".mpd", ".ts?"))
                    )
                    if is_media:
                        norm = normalize_url(url)
                        if norm:
                            self._captured_media.add(norm)
                except Exception:
                    pass

            self._page.on("response", on_response)

            mode = "headed" if self.headed else "headless"
            self.logger.info(
                f"Playwright initialized ({mode} Chromium)"
                + (" + proxy" if self.proxy else "")
                + " + network media interception enabled"
            )
        except Exception as e:
            self.logger.warning(f"Failed to initialize Playwright: {e}. Falling back to requests-only mode.")
            self.use_playwright = False
            self._playwright = None
            self._context = None
            self._browser = None
            self._page = None

    def _close_playwright(self):
        try:
            if self._page:
                self._page.close()
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._page = self._context = self._browser = self._playwright = None
        self._captured_media.clear()

    def _human_like_mouse(self, page, duration: float = 1.5, start_x: int = None, start_y: int = None):
        """Advanced human-like mouse simulation with variable speed, small pauses, and slight curves.
        This helps bypass behavioral detection better than pure random moves.
        """
        try:
            end_time = time.time() + duration
            x = start_x if start_x is not None else random.randint(300, 900)
            y = start_y if start_y is not None else random.randint(200, 550)

            while time.time() < end_time:
                # Simulate natural hand movement with slight curve
                target_x = x + random.randint(-180, 180)
                target_y = y + random.randint(-120, 120)
                target_x = max(80, min(1250, target_x))
                target_y = max(60, min(720, target_y))

                steps = random.randint(5, 20)
                for i in range(1, steps + 1):
                    progress = i / steps
                    # Simple easing for more human feel
                    ease = progress * progress * (3 - 2 * progress)  # smoothstep
                    curr_x = x + (target_x - x) * ease + random.randint(-2, 2)
                    curr_y = y + (target_y - y) * ease + random.randint(-2, 2)
                    page.mouse.move(curr_x, curr_y)
                    time.sleep(random.uniform(0.008, 0.035))  # variable micro-speed

                x, y = target_x, target_y

                # Occasional small "thinking" pause or micro-adjustment (very human)
                if random.random() < 0.25:
                    time.sleep(random.uniform(0.15, 0.45))
                    # tiny jitter as if adjusting hand
                    for _ in range(random.randint(1, 3)):
                        page.mouse.move(x + random.randint(-8, 8), y + random.randint(-5, 5))
                        time.sleep(random.uniform(0.04, 0.12))

        except Exception:
            pass

    def _bezier_mouse_move(self, page, start, end, duration=0.8, wobble=15):
        """Simulate a smooth human-like curved mouse path using a simple quadratic bezier."""
        try:
            sx, sy = start
            ex, ey = end
            # Control point with some randomness for natural curve
            cx = (sx + ex) / 2 + random.randint(-wobble, wobble)
            cy = (sy + ey) / 2 + random.randint(-wobble, wobble)

            steps = max(8, int(duration * 25))
            for t in range(steps + 1):
                p = t / steps
                # Quadratic bezier
                bx = (1-p)**2 * sx + 2*(1-p)*p * cx + p**2 * ex
                by = (1-p)**2 * sy + 2*(1-p)*p * cy + p**2 * ey
                # Add tiny human wobble
                bx += random.randint(-2, 2)
                by += random.randint(-2, 2)
                page.mouse.move(bx, by)
                time.sleep(duration / steps + random.uniform(-0.005, 0.015))
        except Exception:
            # Fallback to straight move
            page.mouse.move(ex, ey)

    def _act_like_reading_page(self, page, duration: float = 2.0):
        """Simulate a human slowly reading/scanning the page content.
        This is very effective against advanced behavioral CF checks.
        """
        try:
            # Slow deliberate mouse over "text areas"
            for _ in range(random.randint(3, 7)):
                x = random.randint(150, 1100)
                y = random.randint(120, 580)
                page.mouse.move(x, y, steps=random.randint(8, 18))
                time.sleep(random.uniform(0.4, 1.1))

                # Small vertical scan as if reading lines
                for dy in range(0, random.randint(40, 140), 18):
                    page.mouse.move(x + random.randint(-15, 15), y + dy)
                    time.sleep(random.uniform(0.06, 0.18))

            time.sleep(random.uniform(0.3, 0.7))
        except Exception:
            pass

    def _random_distraction_actions(self, page, count: int = 2):
        """Occasional 'normal user' actions on the page (hover links, small scrolls) to look less focused only on the challenge.
        This is advanced behavioral camouflage.
        """
        try:
            for _ in range(count):
                action = random.choice(["hover_link", "small_scroll", "pause"])
                if action == "hover_link":
                    links = page.locator("a").all()
                    if links:
                        random.choice(links[:min(5, len(links))]).hover()
                        time.sleep(random.uniform(0.3, 0.8))
                elif action == "small_scroll":
                    page.evaluate(f"window.scrollBy(0, {random.randint(-80, 120)})")
                    time.sleep(random.uniform(0.2, 0.6))
                else:
                    time.sleep(random.uniform(0.4, 1.0))
                self._human_like_mouse(page, 0.5)
        except Exception:
            pass

    def _human_keyboard_focus(self, page_or_frame, times: int = 1):
        """Simulate human using keyboard to focus elements (Tab, Space, etc.).
        Useful for checkbox challenges that respond to keyboard.
        """
        try:
            for _ in range(times):
                # Random small tabs as if navigating
                for _ in range(random.randint(0, 2)):
                    page_or_frame.keyboard.press("Tab")
                    time.sleep(random.uniform(0.1, 0.35))
                # Sometimes press Space to toggle checkbox
                if random.random() < 0.7:
                    page_or_frame.keyboard.press("Space")
                    time.sleep(random.uniform(0.2, 0.5))
                time.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass

    def _handle_cloudflare_challenge(self, page) -> bool:
        """
        Highly advanced open-source Cloudflare / Turnstile solver.
        Goal: simulate a real distracted human as much as possible so the challenge
        passes with minimal or zero manual clicking.

        Techniques:
        - Multiple stages of increasing "humanity"
        - Natural curved mouse movements + reading simulation
        - Deep iframe interaction
        - Deliberate hover + slow click on the checkbox
        - Lots of random micro-behaviors between actions
        - Persistent retries with varied timing
        """
        try:
            def is_still_challenge(p):
                try:
                    t = (p.title() or "").lower()
                    c = p.content().lower()[:4000]
                    return any(kw in t or kw in c for kw in [
                        "just a moment", "verify you are human", "checking your browser",
                        "cf-challenge", "attention required", "turnstile", "please wait"
                    ])
                except:
                    return False

            if not is_still_challenge(page):
                return True

            self.logger.info("Cloudflare challenge detected. Running advanced human-simulation solver...")

            # === STAGE 1: Pure passive human presence (often enough for light challenges) ===
            self.logger.info("  Stage 1: Acting like a normal person reading the page...")
            for _ in range(random.randint(2, 4)):
                self._act_like_reading_page(page, duration=random.uniform(1.8, 3.2))
                self._human_like_mouse(page, duration=random.uniform(1.5, 2.8))
                self._random_distraction_actions(page, random.randint(1, 3))
                # Occasional random scroll as if deciding whether to continue
                if random.random() < 0.6:
                    page.evaluate(f"window.scrollBy(0, {random.randint(-250, 550)})")
                    time.sleep(random.uniform(0.6, 1.4))

            if not is_still_challenge(page):
                self.logger.info("Cloudflare challenge solved during passive human simulation!")
                return True

            # === STAGE 2: Targeted interaction on main page + natural approach ===
            self.logger.info("  Stage 2: Natural approach + clicking visible elements...")
            for attempt in range(4):
                # Arrive at the challenge area "naturally"
                self._human_like_mouse(page, duration=random.uniform(1.0, 2.0), start_x=600, start_y=350)

                challenge_selectors = [
                    'text=Verify you are human',
                    'text=I am not a robot',
                    'input[type="checkbox"]',
                    '[role="checkbox"]',
                    '.cf-turnstile',
                    'button:has-text("Continue")',
                    'text=Click to verify',
                ]

                clicked_something = False
                for selector in challenge_selectors:
                    try:
                        loc = page.locator(selector).first
                        if loc and loc.is_visible(timeout=1200):
                            # Approach slowly and human-like
                            box = loc.bounding_box()
                            if box:
                                # Come from a random nearby point
                                approach_x = box["x"] + random.uniform(-40, box["width"] + 40)
                                approach_y = box["y"] + random.uniform(-30, box["height"] + 30)
                                page.mouse.move(approach_x, approach_y, steps=random.randint(6, 14))
                                time.sleep(random.uniform(0.2, 0.5))

                            # Hover for a realistic amount of time (humans don't click instantly)
                            loc.hover()
                            time.sleep(random.uniform(0.35, 1.1))

                            loc.click(delay=random.randint(70, 280))
                            self.logger.info(f"  Clicked visible challenge element: {selector}")
                            clicked_something = True
                            time.sleep(random.uniform(2.2, 4.5))
                            self._human_like_mouse(page, 1.2)
                            break
                    except Exception:
                        continue

                if clicked_something:
                    # After click, do more random human stuff while verification happens
                    for _ in range(random.randint(3, 6)):
                        self._human_like_mouse(page, 0.7)
                        time.sleep(random.uniform(0.4, 1.0))

                # Check success frequently
                for _ in range(12):
                    time.sleep(random.uniform(0.6, 1.1))
                    if not is_still_challenge(page):
                        self.logger.info("Cloudflare challenge solved after element interaction!")
                        self._act_like_reading_page(page, 1.0)
                        return True

            # === STAGE 3: Aggressive but still human iframe focus ===
            self.logger.info("  Stage 3: Focusing on Cloudflare iframe (most common location)...")
            try:
                for frame_attempt in range(5):
                    frames = [f for f in page.frames if f != page.main_frame]
                    cf_frame = None
                    for f in frames:
                        fu = (f.url or "").lower()
                        if any(x in fu for x in ["challenges", "turnstile", "cloudflare", "hcaptcha"]):
                            cf_frame = f
                            break

                    if cf_frame:
                        self.logger.info(f"    Interacting with CF iframe (attempt {frame_attempt + 1})...")
                        # Spend time "looking at" the challenge
                        self._human_like_mouse(cf_frame, duration=random.uniform(0.8, 1.8))

                        # Use bezier to approach the checkbox area naturally
                        try:
                            cb = cf_frame.locator('input[type="checkbox"], [role="checkbox"]').first
                            if cb and cb.is_visible(timeout=1500):
                                box = cb.bounding_box()
                                if box:
                                    start = (box["x"] - 70 + random.randint(-15, 15), box["y"] + random.randint(-5, 15))
                                    end = (box["x"] + 9, box["y"] + 9)
                                    self._bezier_mouse_move(cf_frame, start, end, duration=random.uniform(0.7, 1.3))
                                    # Hover as if reading the "I'm not a robot" text
                                    cf_frame.mouse.move(box["x"] - 25, box["y"] + 4)
                                    time.sleep(random.uniform(0.35, 0.85))
                        except Exception:
                            pass

                        # Try to find and properly engage the checkbox
                        checkbox_selectors = [
                            'input[type="checkbox"]',
                            '[role="checkbox"]',
                            '.ctp-checkbox',
                            'div[role="presentation"] input',
                        ]

                        for sel in checkbox_selectors:
                            try:
                                cb = cf_frame.locator(sel).first
                                if cb and cb.is_visible(timeout=1500):
                                    # Very deliberate human approach
                                    box = cb.bounding_box()
                                    if box:
                                        # Hover nearby first (as if reading the text next to checkbox)
                                        cf_frame.mouse.move(box["x"] - 45, box["y"] + 8, steps=12)
                                        time.sleep(random.uniform(0.4, 0.9))
                                        # Then move onto the box slowly
                                        cf_frame.mouse.move(box["x"] + 10, box["y"] + 10, steps=8)
                                        time.sleep(random.uniform(0.25, 0.6))

                                    cb.hover()
                                    time.sleep(random.uniform(0.3, 0.7))
                                    cb.click(delay=random.randint(120, 350))
                                    self.logger.info(f"    Clicked checkbox in iframe using {sel}")
                                    time.sleep(random.uniform(4.0, 7.0))

                                    # After click, do convincing post-click human behavior
                                    self._human_like_mouse(cf_frame, 1.5)
                                    self._act_like_reading_page(page, 1.2)

                                    # Check if it passed
                                    for _ in range(15):
                                        time.sleep(random.uniform(0.7, 1.3))
                                        if not is_still_challenge(page):
                                            self.logger.info("Cloudflare challenge solved via iframe checkbox!")
                                            return True
                            except Exception:
                                pass

                    # If still challenged, do more random convincing behavior
                    self._human_like_mouse(page, duration=random.uniform(1.5, 3.0))
                    page.evaluate(f"window.scrollBy(0, {random.randint(-180, 420)})")
                    time.sleep(random.uniform(0.5, 1.2))

            except Exception as e:
                self.logger.debug(f"Iframe stage error: {e}")

            # === STAGE 4: Extended "I'm just a normal user" behavior ===
            self.logger.info("  Stage 4: Extended realistic behavior (last automated attempt)...")
            for _ in range(random.randint(8, 14)):
                action = random.choice(["mouse", "scroll", "read", "jitter"])
                if action == "mouse":
                    self._human_like_mouse(page, random.uniform(1.0, 2.5))
                elif action == "scroll":
                    page.evaluate(f"window.scrollBy(0, {random.randint(-300, 500)})")
                    time.sleep(random.uniform(0.4, 1.3))
                elif action == "read":
                    self._act_like_reading_page(page, random.uniform(1.2, 2.5))
                else:
                    # Small random jitter + pause (very common human micro-behavior)
                    for _ in range(random.randint(2, 5)):
                        page.mouse.move(
                            random.randint(200, 1000),
                            random.randint(150, 600)
                        )
                        time.sleep(random.uniform(0.1, 0.35))
                    time.sleep(random.uniform(0.8, 2.2))

                if not is_still_challenge(page):
                    self.logger.info("Cloudflare challenge cleared during extended human behavior!")
                    return True

            # === FINAL: Only fall back to manual if user explicitly enabled --cf-manual ===
            if getattr(self, 'cf_manual_help', False):
                self.logger.warning(
                    "All automated human-simulation stages exhausted. "
                    "Please manually click the Cloudflare checkbox in the browser window if visible. "
                    "Waiting up to 45 seconds..."
                )
                for _ in range(45):
                    time.sleep(1)
                    if not is_still_challenge(page):
                        self.logger.info("Cloudflare challenge solved after manual intervention!")
                        return True
                self.logger.warning("Manual wait timed out. Proceeding anyway (may still work).")
            else:
                self.logger.info("All automated stages completed. Proceeding with extraction...")

            return True

        except Exception as e:
            self.logger.debug(f"Challenge handling error: {e}")
            return False

    def _is_allowed_by_robots(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        try:
            p = urlparse(url)
            netloc = p.netloc.lower()
            if netloc not in self.robots_allowed:
                robots_url = f"{p.scheme}://{netloc}/robots.txt"
                rp = robotparser.RobotFileParser()
                rp.set_url(robots_url)
                try:
                    rp.read()
                    self.robots_allowed[netloc] = rp.can_fetch(self.user_agent, url)
                except Exception:
                    # If robots.txt unreachable, be permissive but polite
                    self.robots_allowed[netloc] = True
            return self.robots_allowed.get(netloc, True)
        except Exception:
            return True

    def _should_crawl(self, url: str, current_depth: int) -> bool:
        if current_depth > self.depth:
            return False

        norm = normalize_url(url)
        if norm in self.visited:
            return False
        if not url.startswith(("http://", "https://")):
            return False

        p = urlparse(url)
        netloc = p.netloc.lower()

        if self.allowed_domains:
            if not any(netloc.endswith(d) or d.endswith(netloc) for d in self.allowed_domains):
                return False
        elif self.same_domain:
            if not is_same_domain(url, self.base_domain):
                return False

        if not self._is_allowed_by_robots(url):
            self.logger.debug(f"Blocked by robots.txt: {url}")
            return False

        return True

    def _respect_per_domain_delay(self, url: str):
        """Simple per-domain rate limiting for extra politeness."""
        try:
            domain = urlparse(url).netloc.lower()
            last = self._domain_last_access.get(domain, 0)
            now = time.time()
            min_gap = random.uniform(*self.delay_range) * 0.6
            if now - last < min_gap:
                sleep_for = min_gap - (now - last)
                time.sleep(sleep_for)
            self._domain_last_access[domain] = time.time()
        except Exception:
            pass

    def _get_page_html(self, url: str) -> Optional[str]:
        """Fetch page HTML. Uses Playwright if enabled (with network capture), else robust requests session."""
        self._respect_per_domain_delay(url)

        if self.use_playwright and self._page is not None:
            try:
                # Clear previous captures for this page
                before = len(self._captured_media)
                self._page.goto(url, wait_until="domcontentloaded", timeout=45000)

                # Try to wait for the page to become more stable (helps with CF challenges etc.)
                try:
                    self._page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                # === Open-source code-driven Cloudflare handling ===
                # Try multiple times with improved simulation (handles iframes + persistent cookies)
                solved = self._handle_cloudflare_challenge(self._page)
                if not solved:
                    # One more aggressive pass
                    time.sleep(2)
                    self._handle_cloudflare_challenge(self._page)

                # Give time after handling + one final human-like action
                time.sleep(random.uniform(1.2, 2.8))
                self._human_like_mouse(self._page, 0.7)

                html = self._page.content()

                # Merge any new media we saw on the wire
                after = len(self._captured_media)
                if after > before:
                    self.logger.debug(f"Playwright network capture found {after - before} additional media URL(s) on this page")

                return html
            except Exception as e:
                self.logger.warning(f"Playwright fetch failed for {url}: {e}")
                # fall through to requests

        # Robust requests path (already has retries + proxy configured)
        try:
            resp = self.session.get(url, timeout=22, allow_redirects=True)
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "")
            if "text/html" not in ctype and "application/xhtml" not in ctype:
                if not resp.text.strip().startswith("<"):
                    return None
            return resp.text
        except requests.exceptions.RequestException as e:
            self.logger.debug(f"Request error {url}: {e}")
            return None
        except Exception as e:
            self.logger.debug(f"Unexpected fetch error {url}: {e}")
            return None

    def _extract_from_soup(self, soup: BeautifulSoup, base_url: str) -> Set[str]:
        """Extract candidate video URLs from parsed HTML."""
        candidates: Set[str] = set()

        # 1. <video> and <source> tags
        for tag in soup.find_all(["video", "source", "audio"]):
            for attr in ("src", "data-src", "data-url", "data-lazy-src", "data-original"):
                val = tag.get(attr)
                if val:
                    abs_url = make_absolute(base_url, val)
                    if abs_url and is_video_url(abs_url):
                        candidates.add(abs_url)

        # 2. <a> tags that point to video files
        for a in soup.find_all("a", href=True):
            href = a["href"]
            abs_url = make_absolute(base_url, href)
            if abs_url and is_video_url(abs_url):
                candidates.add(abs_url)

        # 3. Common data attributes on many elements (data-src, data-video, etc.)
        for tag in soup.find_all(True):
            for attr, val in tag.attrs.items():
                if not isinstance(val, str):
                    continue
                if any(k in attr.lower() for k in ("src", "url", "video", "source", "file", "stream")):
                    abs_url = make_absolute(base_url, val)
                    if abs_url and is_video_url(abs_url):
                        candidates.add(abs_url)

        # 4. iframe src that might be video embeds (youtube, vimeo, etc.)
        for iframe in soup.find_all("iframe", src=True):
            src = make_absolute(base_url, iframe["src"])
            if src:
                # We don't mark as direct video here; yt-dlp will handle many embed URLs
                # Only add if it really looks like a video file
                if is_video_url(src):
                    candidates.add(src)

        return candidates

    def _extract_from_raw_source(self, html: str, base_url: str) -> Set[str]:
        """Aggressive regex extraction over the full raw HTML/JS source."""
        candidates: Set[str] = set()

        # Direct video extensions
        for m in VIDEO_URL_RE.finditer(html):
            u = m.group(1)
            if u.startswith("//"):
                u = "https:" + u
            abs_url = make_absolute(base_url, u)
            if abs_url and is_video_url(abs_url):
                candidates.add(abs_url)

        for m in SRC_LIKE_RE.finditer(html):
            u = m.group(1)
            if u.startswith("//"):
                u = "https:" + u
            abs_url = make_absolute(base_url, u)
            if abs_url and is_video_url(abs_url):
                candidates.add(abs_url)

        for m in GENERIC_VIDEO_RE.finditer(html):
            u = m.group(0)
            if u.startswith("//"):
                u = "https:" + u
            abs_url = make_absolute(base_url, u)
            if abs_url and is_video_url(abs_url):
                candidates.add(abs_url)

        # Try to catch JSON-like "url": "https://...mp4"
        try:
            # Very loose: look for http(s) strings that end with video-ish inside quotes
            loose = re.findall(r'["\'](https?://[^"\']+?(?:mp4|webm|m3u8|mpd)[^"\']*?)["\']', html, re.I)
            for u in loose:
                abs_url = make_absolute(base_url, u)
                if abs_url and is_video_url(abs_url):
                    candidates.add(abs_url)
        except Exception:
            pass

        return candidates

    def _extract_videos(self, html: str, base_url: str) -> List[str]:
        """Combine soup + raw source + (if Playwright) network-captured media."""
        soup = BeautifulSoup(html, "lxml")
        from_soup = self._extract_from_soup(soup, base_url)
        from_raw = self._extract_from_raw_source(html, base_url)
        combined = from_soup | from_raw

        # === NEW: Merge real network requests captured by Playwright ===
        if self.use_playwright and self._captured_media:
            for u in self._captured_media:
                if is_video_url(u):
                    combined.add(u)

        # Normalize and dedup at extraction time
        normalized = set()
        for u in combined:
            norm = normalize_url(u)
            if norm and is_video_url(norm):
                normalized.add(norm)
        return sorted(normalized)

    def _record_videos(self, video_urls: List[str], source_page: str, page_title: Optional[str] = None):
        """Add newly discovered videos to the result list (deduped).
        Optionally attaches a page_title as a hint for better filenames later.
        """
        for url in video_urls:
            norm = normalize_url(url)
            if norm in self.seen_video_urls:
                continue
            self.seen_video_urls.add(norm)

            vtype = classify_video_type(url)
            entry = {
                "url": norm,
                "source_page": source_page,
                "discovered_at": now_iso(),
                "status": "pending",
                "type": vtype,
            }
            if page_title:
                entry["title"] = page_title  # Will be used as filename hint if yt-dlp doesn't provide a better one
            self.videos.append(entry)
            self.logger.info(f"  [+] Video found ({vtype}): {norm[:110]}")

    def crawl(self) -> List[Dict[str, Any]]:
        """Main BFS crawl. Returns the list of discovered video entries."""
        self.logger.info(f"Starting crawl: {self.start_url}")
        self.logger.info(f"  depth={self.depth} max_pages={self.max_pages} playwright={self.use_playwright}")

        self.queue.append((self.start_url, 0))
        pages_crawled = 0

        try:
            while self.queue and pages_crawled < self.max_pages:
                url, current_depth = self.queue.popleft()
                norm_url = normalize_url(url)

                if norm_url in self.visited:
                    continue
                if not self._should_crawl(url, current_depth):
                    continue

                self.visited.add(norm_url)
                # Also mark the raw form to avoid re-adding variants quickly
                self.visited.add(url)
                pages_crawled += 1

                self.logger.info(f"[{pages_crawled}/{self.max_pages}] Crawling (d={current_depth}): {url}")

                html = self._get_page_html(url)
                if not html:
                    polite_sleep(*self.delay_range)
                    continue

                # Extract page title once (used as fallback for filenames)
                page_title = extract_title_from_html(html)

                # Extract videos on this page
                video_urls = self._extract_videos(html, url)
                if video_urls:
                    self._record_videos(video_urls, url, page_title=page_title)

                # Find internal links for further crawling (improved coverage)
                if current_depth < self.depth:
                    try:
                        soup = BeautifulSoup(html, "lxml")
                        links_found = set()

                        # Standard links
                        for tag in soup.find_all(["a", "link"], href=True):
                            links_found.add(tag["href"])

                        # Some sites put next pages in data attributes or buttons
                        for tag in soup.find_all(True):
                            for attr in ("data-href", "data-url", "data-link"):
                                if tag.get(attr):
                                    links_found.add(tag[attr])

                        for href in links_found:
                            link = make_absolute(url, href)
                            if link and self._should_crawl(link, current_depth + 1):
                                norm = normalize_url(link)
                                if norm and norm not in self.visited:
                                    self.queue.append((link, current_depth + 1))
                    except Exception as e:
                        self.logger.debug(f"Link extraction error on {url}: {e}")

                polite_sleep(*self.delay_range)

        except KeyboardInterrupt:
            self.logger.warning("Crawl interrupted by user.")
        finally:
            self._close_playwright()

        self.logger.info(f"Crawl finished. Pages visited: {len(self.visited)}")
        self.logger.info(f"Total unique videos discovered: {len(self.videos)}")

        # Optionally persist immediately
        if self.output_path:
            from utils import save_video_list
            meta = {
                "start_url": self.start_url,
                "crawled_at": now_iso(),
                "pages_visited": len(self.visited),
                "depth": self.depth,
                "max_pages": self.max_pages,
                "playwright_used": self.use_playwright,
            }
            save_video_list(self.output_path, self.videos, meta=meta)
            self.logger.info(f"Saved results to {self.output_path}")

        return self.videos

    @staticmethod
    def load_existing(path: Path) -> List[Dict[str, Any]]:
        """Convenience loader."""
        from utils import load_video_list
        return load_video_list(path)


def crawl_site(
    start_url: str,
    output_path: Optional[Path] = None,
    depth: int = 3,
    max_pages: int = 200,
    use_playwright: bool = False,
    same_domain: bool = True,
    allowed_domains: Optional[List[str]] = None,
    logger: Optional[Any] = None,
    proxy: Optional[str] = None,
    respect_robots: bool = True,
    cf_manual_help: bool = False,
    headed: bool = False,
) -> List[Dict[str, Any]]:
    """High-level helper used by CLI/GUI."""
    crawler = Crawler(
        start_url=start_url,
        depth=depth,
        max_pages=max_pages,
        use_playwright=use_playwright,
        same_domain=same_domain,
        allowed_domains=allowed_domains,
        logger=logger,
        output_path=output_path,
        proxy=proxy,
        respect_robots=respect_robots,
        cf_manual_help=cf_manual_help,
        headed=headed,
    )
    return crawler.crawl()
