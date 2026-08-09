"""
VidHarvester - Streamlit GUI

Run with:
    streamlit run gui.py

Features:
- Crawl configuration
- Live log area during crawl
- Video discovery table with selection
- Download selected / all pending
- Status overview + exports
- Settings sidebar
"""
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
import threading
from queue import Queue, Empty

import streamlit as st
import pandas as pd

from utils import (
    setup_logging, load_video_list, save_video_list,
    export_urls_txt, export_csv, filter_videos, merge_video_lists, now_iso
)
from crawler import crawl_site
from downloader import Downloader


# ---------------- Session helpers ----------------

DEFAULT_LIST_PATH = Path("gui_videos.json")

def get_state():
    ss = st.session_state
    if "videos" not in ss:
        ss.videos = []
    if "log_messages" not in ss:
        ss.log_messages = []
    if "current_list_path" not in ss:
        ss.current_list_path = str(DEFAULT_LIST_PATH)
    if "selected_urls" not in ss:
        ss.selected_urls = set()
    if "download_in_progress" not in ss:
        ss.download_in_progress = False
    if "crawl_in_progress" not in ss:
        ss.crawl_in_progress = False
    if "download_progress" not in ss:
        ss.download_progress = {"current": 0, "total": 0, "message": ""}
    return ss


def log(msg: str):
    ss = st.session_state
    ss.log_messages.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
    if len(ss.log_messages) > 90:
        ss.log_messages = ss.log_messages[-90:]


def load_list(path: str):
    p = Path(path)
    videos = load_video_list(p)
    st.session_state.videos = videos
    st.session_state.current_list_path = str(p)
    st.session_state.selected_urls = set()
    log(f"Loaded {len(videos)} videos from {p}")


def save_current_list():
    p = Path(st.session_state.current_list_path)
    save_video_list(p, st.session_state.videos, meta={"saved_from_gui_at": now_iso()})
    log(f"Saved list to {p}")


def add_new_videos(new_videos: List[Dict[str, Any]]):
    existing = st.session_state.videos or []
    merged = merge_video_lists(existing, new_videos)
    st.session_state.videos = merged
    log(f"Merged discoveries: total now {len(merged)}")


def toggle_selection(url: str, checked: bool):
    if checked:
        st.session_state.selected_urls.add(url)
    else:
        st.session_state.selected_urls.discard(url)


def clear_selection():
    st.session_state.selected_urls.clear()


# ---------------- Background workers ----------------

def _background_crawl(url: str, depth: int, max_pages: int, use_browser: bool, cross_domain: bool,
                      proxy: Optional[str], user_agent: Optional[str], result_queue: Queue):
    """Runs in a thread so Streamlit UI stays responsive."""
    try:
        import logging
        gui_logger = logging.getLogger("vidharvester.gui.crawl")
        gui_logger.setLevel(logging.INFO)
        gui_logger.handlers = []

        class LogHandler(logging.Handler):
            def emit(self, record):
                try:
                    result_queue.put(("log", record.getMessage()))
                except Exception:
                    pass
        gui_logger.addHandler(LogHandler())

        result_queue.put(("log", f"Starting crawl: {url} (depth={depth})" + (f" via proxy" if proxy else "")))

        new_videos = crawl_site(
            start_url=url,
            output_path=None,
            depth=depth,
            max_pages=max_pages,
            use_playwright=use_browser,
            same_domain=not cross_domain,
            logger=gui_logger,
            proxy=proxy,
        )

        result_queue.put(("crawl_done", new_videos))
    except Exception as e:
        result_queue.put(("error", str(e)))


def _background_download(videos_ref: List[Dict], selected_urls: List[str] | None, result_queue: Queue):
    """Runs downloads in background thread."""
    try:
        def progress_cb(current, total, url):
            result_queue.put(("progress", {"current": current, "total": total, "url": url[:90]}))

        dl = Downloader(
            videos=videos_ref,
            output_root=Path("downloads"),
            proxy=ss.get("proxy") or None,
            user_agent=ss.get("ua") or None,
        )
        # Route logs into the queue
        class QLogger:
            def info(self, m): result_queue.put(("log", str(m)))
            def warning(self, m): result_queue.put(("log", str(m)))
            def error(self, m): result_queue.put(("log", "ERROR: " + str(m)))
            def debug(self, m): pass
        dl.logger = QLogger()

        result_queue.put(("log", f"Starting download of {len([v for v in videos_ref if v.get('status')=='pending'])} videos..."))

        stats = dl.download_all(
            only_pending=True,
            selected_urls=selected_urls,
            progress_callback=progress_cb
        )
        result_queue.put(("download_done", stats))
    except Exception as e:
        result_queue.put(("error", str(e)))


# ---------------- Crawl ----------------

def run_crawl(url: str, depth: int, max_pages: int, use_browser: bool, cross_domain: bool,
              proxy: Optional[str] = None, user_agent: Optional[str] = None):
    ss = st.session_state
    if ss.crawl_in_progress:
        return

    ss.crawl_in_progress = True
    ss.log_messages = []  # fresh log for this run
    log(f"Launching background crawl for {url}...")

    q: Queue = Queue()
    t = threading.Thread(
        target=_background_crawl,
        args=(url, depth, max_pages, use_browser, cross_domain, proxy, user_agent, q),
        daemon=True
    )
    t.start()

    # Poll the queue while showing progress
    progress_placeholder = st.empty()
    log_placeholder = st.empty()

    while t.is_alive() or not q.empty():
        try:
            kind, payload = q.get(timeout=0.15)
            if kind == "log":
                log(payload)
            elif kind == "crawl_done":
                new_videos = payload
                add_new_videos(new_videos)
                save_current_list()
                log(f"Crawl complete. +{len(new_videos)} new videos (total {len(ss.videos)})")
                ss.crawl_in_progress = False
                break
            elif kind == "error":
                log(f"Crawl error: {payload}")
                st.error(f"Crawl failed: {payload}")
                ss.crawl_in_progress = False
                break
        except Empty:
            pass

        # Update UI
        with progress_placeholder.container():
            st.info("Crawling in background... (UI remains responsive)")
        with log_placeholder.container():
            if ss.log_messages:
                st.code("\n".join(ss.log_messages[-25:]), language="text")

    progress_placeholder.empty()
    log_placeholder.empty()
    ss.crawl_in_progress = False


# ---------------- Download ----------------

def run_download(selected_only: bool):
    ss = st.session_state
    if ss.download_in_progress:
        return

    videos = ss.videos
    if not videos:
        st.warning("No videos loaded.")
        return

    selected_list = list(ss.selected_urls) if selected_only else None
    if selected_only and not selected_list:
        st.warning("No videos selected.")
        return

    ss.download_in_progress = True
    ss.download_progress = {"current": 0, "total": 0, "message": "Starting..."}

    q: Queue = Queue()
    t = threading.Thread(
        target=_background_download,
        args=(videos, selected_list, q),
        daemon=True
    )
    t.start()

    progress_bar = st.progress(0)
    status_text = st.empty()
    log_area = st.empty()

    while t.is_alive() or not q.empty():
        try:
            kind, payload = q.get(timeout=0.1)
            if kind == "log":
                log(payload)
            elif kind == "progress":
                ss.download_progress = payload
                pct = int((payload["current"] / max(payload["total"], 1)) * 100)
                progress_bar.progress(min(pct, 100))
                status_text.text(f"{payload['current']}/{payload['total']} — {payload['url']}")
            elif kind == "download_done":
                stats = payload
                log(f"Download finished. Downloaded: {stats.get('downloaded',0)} | Failed: {stats.get('failed',0)}")
                save_current_list()
                ss.download_in_progress = False
                break
            elif kind == "error":
                log(f"Download error: {payload}")
                st.error(f"Download failed: {payload}")
                ss.download_in_progress = False
                break
        except Empty:
            pass

        # Live log tail
        with log_area.container():
            if ss.log_messages:
                st.code("\n".join(ss.log_messages[-18:]), language="text")

    progress_bar.empty()
    status_text.empty()
    log_area.empty()
    ss.download_in_progress = False
    # Force a clean rerun so table reflects new statuses
    st.rerun()


# ---------------- GUI Layout ----------------

st.set_page_config(page_title="VidHarvester", page_icon="🎥", layout="wide")
st.title("🎥 VidHarvester")
st.caption("Video Link Harvester & Downloader — Discover • Review • Download")

state = get_state()

# Sidebar - Settings
with st.sidebar:
    st.header("Settings")

    st.subheader("Crawl")
    start_url = st.text_input("Start URL", value="https://example.com/videos", key="start_url")
    depth = st.slider("Depth", 1, 6, 3, key="depth")
    max_pages = st.number_input("Max pages", 10, 2000, 150, step=10, key="max_pages")
    use_browser = st.checkbox("Use open-source browser (Playwright)", value=False,
                              help="Fully code-controlled browser with advanced multi-stage human simulation for Cloudflare (realistic mouse paths, 'reading' behavior, iframe checkbox interaction). Use --headed in CLI to watch automatic solving. Captures network media. Requires `playwright install`")
    cross_domain = st.checkbox("Allow other domains", value=False)

    proxy = st.text_input("Proxy (optional)", value="", key="proxy",
                          placeholder="http://127.0.0.1:8080 or socks5://...",
                          help="Supports http/https/socks5. Also respects HTTP_PROXY env var.")
    user_agent = st.text_input("Custom User-Agent (optional)", value="", key="ua")

    st.divider()
    st.subheader("Data File")
    list_path = st.text_input("Video list file", value=state.current_list_path, key="list_path")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Load List", use_container_width=True):
            load_list(list_path)
    with col2:
        if st.button("Save List", use_container_width=True):
            save_current_list()

    st.divider()
    if st.button("Clear All Videos", type="secondary", use_container_width=True):
        st.session_state.videos = []
        log("Cleared video list from memory.")
        st.rerun()

# Main area - Actions
col_crawl, col_download, col_export = st.columns([1.1, 1.3, 1])

with col_crawl:
    crawl_disabled = state.crawl_in_progress
    if st.button("🚀 Start / Continue Crawl", type="primary", use_container_width=True, disabled=crawl_disabled):
        if not start_url.strip():
            st.error("Please enter a start URL")
        else:
            run_crawl(
                url=start_url.strip(),
                depth=depth,
                max_pages=int(max_pages),
                use_browser=use_browser,
                cross_domain=cross_domain,
                proxy=(proxy or "").strip() or None,
                user_agent=(user_agent or "").strip() or None,
            )

with col_download:
    dl_col1, dl_col2 = st.columns(2)
    download_disabled = state.download_in_progress

    with dl_col1:
        if st.button("⬇️ Download Selected Pending", use_container_width=True, disabled=download_disabled):
            run_download(selected_only=True)

    with dl_col2:
        if st.button("⬇️ Download ALL Pending", use_container_width=True, disabled=download_disabled):
            run_download(selected_only=False)

with col_export:
    exp_col1, exp_col2, exp_col3 = st.columns(3)
    with exp_col1:
        if st.button("Export JSON", use_container_width=True):
            save_current_list()
            st.success(f"Saved to {state.current_list_path}")
    with exp_col2:
        if st.button("Export TXT (URLs)", use_container_width=True):
            p = export_urls_txt(Path(state.current_list_path), state.videos)
            st.success(f"Wrote {p}")
    with exp_col3:
        if st.button("Export CSV", use_container_width=True):
            p = export_csv(Path(state.current_list_path), state.videos)
            st.success(f"Wrote {p}")

# Status metrics
videos = state.videos or []
total = len(videos)
pending = sum(1 for v in videos if v.get("status") == "pending")
downloaded = sum(1 for v in videos if v.get("status") == "downloaded")
failed = sum(1 for v in videos if v.get("status") == "failed")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Total Videos", total)
m2.metric("Pending", pending, delta_color="off")
m3.metric("Downloaded", downloaded)
m4.metric("Failed", failed)

# Log console
with st.expander("📜 Activity Log (last 80 messages)", expanded=False):
    if state.log_messages:
        st.code("\n".join(state.log_messages), language="text")
    else:
        st.caption("No log messages yet.")

# Video table + selection
st.subheader("Discovered Videos")

if not videos:
    st.info("No videos yet. Enter a URL above and click **Start / Continue Crawl**, or load an existing list.")
else:
    # Apply filters first
    kw = st.text_input("🔍 Filter by keyword (URL or source page)", key="filter_kw")
    fcol1, fcol2, fcol3 = st.columns(3)
    with fcol1:
        status_filter = st.selectbox("Status filter", ["(any)", "pending", "downloaded", "failed"], key="filter_status")
    with fcol2:
        type_filter = st.selectbox("Type filter", ["(any)", "direct", "hls", "dash", "yt-dlp"], key="filter_type")

    filtered_videos = filter_videos(
        videos,
        status=None if status_filter == "(any)" else status_filter,
        vtype=None if type_filter == "(any)" else type_filter,
        keyword=kw or None,
    )

    # Build display table with current selection state
    display_rows = []
    current_selected = st.session_state.selected_urls

    for v in filtered_videos:
        url = v.get("url", "")
        display_rows.append({
            "selected": url in current_selected,
            "url": url,
            "source_page": v.get("source_page", "")[:105],
            "type": v.get("type", ""),
            "status": v.get("status", ""),
            "title_or_file": v.get("title") or v.get("saved_as", "")[:55] or "",
        })

    df = pd.DataFrame(display_rows)

    edited_df = st.data_editor(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "selected": st.column_config.CheckboxColumn("Select", width="small"),
            "url": st.column_config.TextColumn("Video URL", width="large"),
            "source_page": st.column_config.TextColumn("Found On", width="medium"),
            "type": st.column_config.TextColumn("Type", width="small"),
            "status": st.column_config.TextColumn("Status", width="small"),
            "title_or_file": st.column_config.TextColumn("Title / Saved As", width="medium"),
        },
        num_rows="fixed",
        key="video_table_editor",
    )

    # Sync checkbox changes back to our clean selected set
    if edited_df is not None:
        for _, row in edited_df.iterrows():
            url = row["url"]
            if row["selected"]:
                st.session_state.selected_urls.add(url)
            else:
                st.session_state.selected_urls.discard(url)

    st.caption(f"Showing {len(filtered_videos)} / {len(videos)} videos • {len(st.session_state.selected_urls)} selected")

    # Quick select helpers
    qa1, qa2, qa3, qa4 = st.columns(4)
    with qa1:
        if st.button("Select All (filtered)", use_container_width=True):
            for v in filtered_videos:
                if v.get("status") == "pending":
                    st.session_state.selected_urls.add(v["url"])
            st.rerun()
    with qa2:
        if st.button("Select All Pending (filtered)", use_container_width=True):
            for v in filtered_videos:
                if v.get("status") == "pending":
                    st.session_state.selected_urls.add(v["url"])
            st.rerun()
    with qa3:
        if st.button("Clear Selection", use_container_width=True):
            clear_selection()
            st.rerun()
    with qa4:
        if st.button("Invert Selection", use_container_width=True):
            all_urls = {v["url"] for v in filtered_videos}
            st.session_state.selected_urls = all_urls - st.session_state.selected_urls
            st.rerun()

    # Bulk status actions
    st.markdown("**Bulk status actions (applies to currently selected):**")
    b1, b2, b3 = st.columns(3)
    with b1:
        if st.button("Mark Selected → pending", use_container_width=True):
            for v in videos:
                if v.get("url") in st.session_state.selected_urls:
                    v["status"] = "pending"
            save_current_list()
            st.rerun()
    with b2:
        if st.button("Mark Selected → failed (retry later)", use_container_width=True):
            for v in videos:
                if v.get("url") in st.session_state.selected_urls:
                    v["status"] = "failed"
            save_current_list()
            st.rerun()
    with b3:
        if st.button("Clear all selections", use_container_width=True):
            clear_selection()
            st.rerun()

st.divider()
st.caption("Tip: For the best results on complex sites, install Playwright (`pip install playwright && playwright install`) and enable 'Use headless browser'.")

if videos:
    st.caption(f"Current list file: `{state.current_list_path}` — use sidebar to load/save. Background threads keep the UI responsive during long operations.")
