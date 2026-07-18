"""Open the LLM observability dashboard and screenshot it.

Uses a tall viewport so all panels (incl. the bottom row) enter the viewport
on load and issue their lazy query_range calls.
"""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
SHOTS = Path("blog/shots")
with open("blog/dashboard_uuid.txt") as fh:
    UUID = fh.read().strip()


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(storage_state="blog/state.json",
                            viewport={"width": 1680, "height": 2050})
        page = ctx.new_page()
        page.goto(f"{BASE}/dashboard/{UUID}", wait_until="networkidle")
        time.sleep(6)

        # Nudge every scroll container to the bottom then back, to trigger any
        # intersection-observer lazy loads on the lower panels.
        for _ in range(3):
            page.evaluate(
                "() => { document.querySelectorAll('*').forEach(e => {"
                "  if (e.scrollHeight > e.clientHeight) e.scrollTop = e.scrollHeight; }); "
                "  window.scrollTo(0, document.body.scrollHeight); }")
            page.keyboard.press("End")
            time.sleep(3)
        page.evaluate("() => window.scrollTo(0, 0)")
        time.sleep(10)

        page.screenshot(path=str(SHOTS / "05_dashboard.png"), full_page=True)
        print("saved 05_dashboard.png")

        ctx.close()
        b.close()


if __name__ == "__main__":
    main()
