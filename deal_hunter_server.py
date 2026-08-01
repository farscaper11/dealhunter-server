import os
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

CATALOG_URL = "https://www.apple.com/shop/refurbished/mac"
INTERVAL_MINUTES = int(os.getenv("DH_SCAN_INTERVAL_MINUTES", "15"))
DATA_DIR = Path(os.getenv("DH_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def log(message: str) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def scan() -> None:
    log("Opening Apple refurbished catalog.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(
            CATALOG_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        result = (
            f"Checked: {datetime.now().astimezone().isoformat()}\n"
            f"Title: {page.title()}\n"
            f"URL: {page.url}\n"
        )

        print(result, flush=True)
        (DATA_DIR / "last-scan.txt").write_text(result, encoding="utf-8")

        browser.close()


def main() -> None:
    log("Deal Hunter Server starting.")

    while True:
        try:
            scan()
        except Exception as exc:
            log(f"Scan failed: {exc}")

        log(f"Sleeping for {INTERVAL_MINUTES} minutes.")
        time.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
