<div align="center">
  <img src="assets/vidharvester_logo.png" alt="VidHarvester Logo" width="360" />

  # VIDHARVESTER 📹
  ### Automated Video Harvesting Pipeline & Media Crawler

  [![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
  [![UI: PySimpleGUI](https://img.shields.io/badge/Interface-GUI_%26_CLI-green.svg)](https://pysimplegui.readthedocs.io/)
  [![Crawler: Playwright](https://img.shields.io/badge/Crawler-Playwright-orange.svg)](https://playwright.dev/)
  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

  *Concurrent media crawler · Batch video extractor · GUI & CLI modes · Resilience logging*
</div>

---

## 📋 Overview

**VidHarvester** is an open-source video crawling and media download pipeline. Featuring both a desktop GUI and command-line execution, VidHarvester parses target media platforms, extracts high-resolution video streams, bypasses lazy-load pagination, and manages batch downloads with error recovery and progress tracking.

---

## ⚡ Quick Start & Usage

```bash
# 1. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 2. Launch GUI
python3 gui.py

# OR run via CLI
python3 main.py --url "https://target-media-site.com" --out ./downloads/
```

---

## 📜 License

This project is licensed under the **MIT License**. See the [LICENSE](LICENSE) file for details.
