"""Capture retry-tax screenshots into blog/shots2/ (does NOT touch blog #1 shots).

  01_chaos_trace.png     - hero: dropped(ERROR) llm.chat -> retry -> tool -> final
  02_chaos_span_drawer   - the dropped span's attributes (retry.reason=response_dropped)
  03_control_trace.png   - clean baseline: one inference per step, no retry
  04_retrytax_dashboard  - the Retry Tax dashboard (control vs chaos)
"""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
SHOTS = Path("blog/shots2")
SHOTS.mkdir(parents=True, exist_ok=True)
CHAOS = "8e38a788aefda3473a71976f6076772c"
CONTROL = "21f14a959df947630948c1d463619456"
UUID = Path("blog/retrytax_dashboard_uuid.txt").read_text().strip()


def set_time_last_3h(page):
    """Best-effort: open the global time picker and pick a wide preset."""
    for trigger in ("Last 30 minutes", "Last 5 minutes", "Last 1 hour",
                    "Last 15 minutes", "Last 6 hours", "Last 3 hours"):
        try:
            loc = page.get_by_text(trigger, exact=True)
            if loc and loc.count() > 0:
                loc.first.click(timeout=2500)
                time.sleep(1)
                for pick in ("Last 3 hours", "Last 6 hours"):
                    try:
                        opt = page.get_by_text(pick, exact=True)
                        if opt and opt.count() > 0:
                            opt.first.click(timeout=2500)
                            print("time set:", pick)
                            time.sleep(3)
                            return True
                    except Exception:
                        pass
        except Exception:
            pass
    print("time picker: no preset matched (using default)")
    return False


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(storage_state="blog/state.json",
                            viewport={"width": 1680, "height": 1200})
        page = ctx.new_page()

        # 1) Chaos hero trace waterfall
        page.goto(f"{BASE}/trace/{CHAOS}", wait_until="networkidle")
        time.sleep(8)
        page.screenshot(path=str(SHOTS / "01_chaos_trace.png"), full_page=True)
        print("saved 01_chaos_trace.png")
        # 2) Try to open the dropped (first llm.chat) span drawer
        for y in (529, 505, 553, 481):
            try:
                page.mouse.click(230, y)
                time.sleep(2.5)
                page.screenshot(path=str(SHOTS / "02_chaos_span_drawer.png"),
                                full_page=True)
                print(f"saved 02_chaos_span_drawer.png (y={y})")
                break
            except Exception as e:
                print("drawer click err:", e)

        # 3) Control trace waterfall
        page.goto(f"{BASE}/trace/{CONTROL}", wait_until="networkidle")
        time.sleep(8)
        page.screenshot(path=str(SHOTS / "03_control_trace.png"), full_page=True)
        print("saved 03_control_trace.png")

        # 4) Retry Tax dashboard (tall viewport for lazy panels)
        page.set_viewport_size({"width": 1680, "height": 2050})
        page.goto(f"{BASE}/dashboard/{UUID}", wait_until="networkidle")
        time.sleep(5)
        set_time_last_3h(page)
        for _ in range(3):
            page.evaluate(
                "() => { document.querySelectorAll('*').forEach(e => {"
                "  if (e.scrollHeight > e.clientHeight) e.scrollTop = e.scrollHeight; }); "
                "  window.scrollTo(0, document.body.scrollHeight); }")
            page.keyboard.press("End")
            time.sleep(3)
        page.evaluate("() => window.scrollTo(0, 0)")
        time.sleep(8)
        page.screenshot(path=str(SHOTS / "04_retrytax_dashboard.png"), full_page=True)
        print("saved 04_retrytax_dashboard.png")

        ctx.close()
        b.close()


if __name__ == "__main__":
    main()
