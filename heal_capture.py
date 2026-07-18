"""Capture the Self-Healing SRE Sidekick screenshots into docs/shots/.

Hero trace (service = self-healer):
  agent.heal
    -> heal.canary.pre / heal.detect (mcp) / heal.decide (read_incident -> mcp,
       disable_fault_injection) / heal.canary.post / heal.verify (mcp)

  01_heal_trace.png    - the whole agent.heal waterfall (the closed loop)
  02_act_span.png      - tool.disable_fault_injection drawer: the model-chosen ACT
  03_read_incident.png - tool.read_incident drawer: diagnose via the SigNoz MCP server
  04_dashboard.png     - the Self-Healing dashboard (before/after + heal activity)
  05_billshock_trace.png - the bill-shock / runaway-spend agent.heal loop
  06_cost_budget_span.png - tool.set_cost_budget drawer: the model arming the cost kill-switch (policy-gated)
  07_cost_incident.png - tool.read_incident drawer: the cost diagnosis via the SigNoz MCP server
"""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
SHOTS = Path("docs/shots")
SHOTS.mkdir(parents=True, exist_ok=True)
TRACE = "31514172b44f0f64548eec5c897eb35b"
COST_TRACE = "1016e57b2073b2ebedbb013386060b49"
DASH = "019f7410-7d88-7b73-bc25-8be374efeb11"


def open_span(page, label, shot, xy=None):
    try:
        loc = page.get_by_text(label, exact=True)
        if loc and loc.count() > 0:
            loc.first.click(timeout=4000, force=True)
            time.sleep(3)
            page.screenshot(path=str(SHOTS / shot), full_page=True)
            print(f"saved {shot} (force text '{label}')")
            return True
    except Exception as e:  # noqa: BLE001
        print(f"force click '{label}' err: {e}")
    if xy:
        try:
            page.mouse.click(*xy)
            time.sleep(3)
            page.screenshot(path=str(SHOTS / shot), full_page=True)
            print(f"saved {shot} (pixel {xy})")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"pixel click '{label}' err: {e}")
    return False


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(storage_state="blog/state.json",
                            viewport={"width": 1680, "height": 1200})
        page = ctx.new_page()

        # 1) The agent.heal waterfall -- the whole closed loop.
        page.goto(f"{BASE}/trace/{TRACE}", wait_until="networkidle")
        time.sleep(9)
        if "login" in page.url.lower():
            print("WARNING: redirected to login -- blog/state.json is stale.")
        page.screenshot(path=str(SHOTS / "01_heal_trace.png"), full_page=True)
        print("saved 01_heal_trace.png; url:", page.url)

        # 2) The ACT: the remediation the model chose.
        open_span(page, "tool.disable_fault_injection", "02_act_span.png", xy=(310, 837))
        # 3) The DIAGNOSE: reading the incident through the MCP server.
        open_span(page, "tool.read_incident", "03_read_incident.png", xy=(260, 725))

        # 4) The Self-Healing dashboard.
        page.goto(f"{BASE}/dashboard/{DASH}", wait_until="networkidle")
        time.sleep(9)
        page.screenshot(path=str(SHOTS / "04_dashboard.png"), full_page=True)
        print("saved 04_dashboard.png; url:", page.url)

        # 5) Bill-shock: the cost / runaway-spend closed loop.
        page.goto(f"{BASE}/trace/{COST_TRACE}", wait_until="networkidle")
        time.sleep(9)
        page.screenshot(path=str(SHOTS / "05_billshock_trace.png"), full_page=True)
        print("saved 05_billshock_trace.png; url:", page.url)
        # 6) The ACT: the model arming the per-request cost kill-switch (policy-gated).
        open_span(page, "tool.set_cost_budget", "06_cost_budget_span.png", xy=(268, 893))
        # 7) The DIAGNOSE: reading the cost incident through the MCP server.
        page.goto(f"{BASE}/trace/{COST_TRACE}", wait_until="networkidle")
        time.sleep(9)
        open_span(page, "tool.read_incident", "07_cost_incident.png", xy=(260, 753))

        ctx.close()
        b.close()


if __name__ == "__main__":
    main()
