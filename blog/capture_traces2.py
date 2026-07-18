"""Second pass: open a span's attribute drawer + a clean Traces Explorer list."""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
SHOTS = Path("blog/shots")
HERO_TRACE = "a905ce3ebb860c3f01966e31b0c68950"


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(storage_state="blog/state.json",
                            viewport={"width": 1680, "height": 1000})
        page = ctx.new_page()

        # --- Span attribute drawer ---
        page.goto(f"{BASE}/trace/{HERO_TRACE}", wait_until="networkidle")
        time.sleep(8)
        # Click the second llm.chat waterfall row (the long synthesis call).
        try:
            page.mouse.click(210, 641)
            time.sleep(3)
            page.screenshot(path=str(SHOTS / "04_span_attributes.png"), full_page=True)
            print("saved 04_span_attributes.png (row2)")
        except Exception as e:
            print("row2 click err:", e)
        # Fallback: first llm.chat row.
        try:
            page.mouse.click(200, 529)
            time.sleep(3)
            page.screenshot(path=str(SHOTS / "04b_span_attributes.png"), full_page=True)
            print("saved 04b_span_attributes.png (row1)")
        except Exception as e:
            print("row1 click err:", e)

        # --- Traces Explorer: dismiss tooltip, list view, wait ---
        page.goto(f"{BASE}/traces-explorer", wait_until="networkidle")
        time.sleep(4)
        for label in ("Okay", "Got it", "Close"):
            try:
                loc = page.get_by_role("button", name=label)
                if loc and loc.count() > 0:
                    loc.first.click(timeout=2000)
                    print("dismissed:", label)
                    time.sleep(1)
            except Exception:
                pass
        # Switch to List view if a tab exists.
        for label in ("List", "Traces"):
            try:
                loc = page.get_by_text(label, exact=True)
                if loc and loc.count() > 0:
                    loc.first.click(timeout=2000)
                    time.sleep(2)
                    break
            except Exception:
                pass
        time.sleep(8)
        page.screenshot(path=str(SHOTS / "03_traces_explorer.png"), full_page=True)
        print("saved 03_traces_explorer.png")

        ctx.close()
        b.close()


if __name__ == "__main__":
    main()
