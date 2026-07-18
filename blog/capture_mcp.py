"""Capture the self-debug (MCP) screenshots into blog/shots3/.

Hero trace: agent.introspect -> llm.chat -> tool.latency_by_operation ->
mcp.signoz_aggregate_traces -> llm.chat. The agent read its OWN traces through
the SigNoz MCP server.

  01_introspect_trace.png - the whole self-debug waterfall (with the mcp.* span)
  02_mcp_span_drawer.png  - mcp.signoz_aggregate_traces attributes (tools/call, http)
  03_tool_result.png      - tool.latency_by_operation result: the real numbers read
"""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
SHOTS = Path("blog/shots3")
SHOTS.mkdir(parents=True, exist_ok=True)
TRACE = "c467af2b755ffed9650654cd0b50d917"


def open_span(page, label, shot, xy=None):
    """Open a span's detail drawer (force-click the name, else pixel fallback)."""
    try:
        loc = page.get_by_text(label, exact=True)
        if loc and loc.count() > 0:
            loc.first.click(timeout=4000, force=True)
            time.sleep(3)
            page.screenshot(path=str(SHOTS / shot), full_page=True)
            print(f"saved {shot} (force text '{label}')")
            return True
    except Exception as e:
        print(f"force click '{label}' err: {e}")
    if xy:
        try:
            page.mouse.click(*xy)
            time.sleep(3)
            page.screenshot(path=str(SHOTS / shot), full_page=True)
            print(f"saved {shot} (pixel {xy})")
            return True
        except Exception as e:
            print(f"pixel click '{label}' err: {e}")
    return False


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(storage_state="blog/state.json",
                            viewport={"width": 1680, "height": 1200})
        page = ctx.new_page()

        # 1) The self-debug trace waterfall.
        page.goto(f"{BASE}/trace/{TRACE}", wait_until="networkidle")
        time.sleep(9)
        if "login" in page.url.lower():
            print("WARNING: redirected to login -- state.json is stale.")
        page.screenshot(path=str(SHOTS / "01_introspect_trace.png"), full_page=True)
        print("saved 01_introspect_trace.png; url:", page.url)

        # 2) The MCP call span -> attributes (mcp.tool.name, tools/call, http).
        open_span(page, "mcp.signoz_aggregate_traces", "02_mcp_span_drawer.png",
                  xy=(340, 585))

        # 3) The tool span -> tool.result holds the real numbers the agent read.
        open_span(page, "tool.latency_by_operation", "03_tool_result.png",
                  xy=(300, 557))

        ctx.close()
        b.close()


if __name__ == "__main__":
    main()
