"""Discover the real SigNoz API-key endpoint by watching what the Settings UI
calls, and dump settings-related links. Read-only; reuses blog/state.json."""
import os

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
STATE = "blog/state.json"

seen = []


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(
            storage_state=STATE if os.path.exists(STATE) else None,
            viewport={"width": 1400, "height": 900})
        pg = ctx.new_page()

        def on_resp(r):
            u = r.url
            if "/api/" in u and any(t in u.lower() for t in
                                    ("pat", "key", "token", "account", "access")):
                seen.append((r.request.method, r.status, u))

        pg.on("response", on_resp)

        for path in ("/settings/api-keys", "/settings/access-tokens",
                     "/settings/personal-access-tokens", "/settings"):
            try:
                pg.goto(BASE + path, wait_until="networkidle", timeout=30000)
                pg.wait_for_timeout(2500)
                print(f"VISITED {path} -> {pg.url} | title: {pg.title()}")
            except Exception as e:
                print("ERR", path, str(e)[:120])

        # Collect settings links from the nav so we know the exact route.
        try:
            links = pg.eval_on_selector_all(
                "a[href*='settings']",
                "els => [...new Set(els.map(e => e.getAttribute('href')))]")
            print("=== settings links ===")
            for l in links:
                print(" ", l)
        except Exception as e:
            print("links err", str(e)[:120])

        print("=== relevant /api calls (pat|key|token|account|access) ===")
        for m, s, u in seen:
            print(f"  {m} {s} {u}")

        ctx.close(); b.close()


if __name__ == "__main__":
    main()
