"""Create (or reuse) a long-lived SigNoz API key for the MCP server.

Logs into self-hosted SigNoz, then creates a PAT via an in-browser fetch (so
the request hits the same origin/auth the SPA uses). Prints the key value.
Writes nothing except blog/state.json (session reuse); never touches shots/.
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
EMAIL = os.environ.get("SIGNOZ_EMAIL", "admin@signoz.local")
PASSWORD = os.environ.get("SIGNOZ_PASSWORD", "changeme")
STATE = "blog/state.json"
KEY_NAME = "mcp-server"


def ensure_logged_in(page):
    pw = page.locator("input[type='password']").first
    if pw.count() > 0 and pw.is_visible():
        try:
            email_in = page.locator("input[type='email'], input[name='email'], input#email").first
            if email_in.count() > 0:
                email_in.fill(EMAIL)
        except Exception:
            pass
        pw.fill(PASSWORD)
        for label in ("Login", "Log in", "Sign in", "Next", "Continue"):
            b = page.get_by_role("button", name=label)
            if b.count() > 0:
                try:
                    b.first.click(timeout=3000)
                    break
                except Exception:
                    pass
        page.wait_for_timeout(6000)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        kwargs = {"viewport": {"width": 1400, "height": 900}}
        if os.path.exists(STATE):
            kwargs["storage_state"] = STATE
        ctx = browser.new_context(**kwargs)
        page = ctx.new_page()
        page.goto(BASE, wait_until="networkidle", timeout=60000)
        ensure_logged_in(page)
        print("URL:", page.url)

        token = page.evaluate("() => window.localStorage.getItem('AUTH_TOKEN')")
        print("have token:", bool(token))
        if not token:
            print("ERROR: no AUTH_TOKEN after login")
            ctx.close(); browser.close(); return 1

        # List existing PATs (learn the payload shape + avoid dupes).
        existing = page.evaluate(
            """async (tok) => {
                const r = await fetch('/api/v1/pats', {headers: {'Authorization': 'Bearer ' + tok}});
                const t = await r.text();
                return {status: r.status, body: t.slice(0, 1200)};
            }""", token)
        print("GET /pats ->", existing["status"])
        print(existing["body"])

        # Reuse an existing mcp-server key token if present (value only shown on
        # create, so this just reports; we create a fresh one below regardless).
        created = page.evaluate(
            """async (args) => {
                const [tok, name] = args;
                const bodies = [
                    {name, role: 'ADMIN', expiresInDays: 0},
                    {name, role: 'ADMIN', expiresAt: 0},
                    {pat: {name, role: 'ADMIN', expiresInDays: 0}},
                ];
                for (const b of bodies) {
                    const r = await fetch('/api/v1/pats', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tok},
                        body: JSON.stringify(b),
                    });
                    const t = await r.text();
                    if (r.status < 300) return {status: r.status, body: t, sent: b};
                    if (r.status !== 400 && r.status !== 422) return {status: r.status, body: t.slice(0,600), sent: b};
                }
                return {status: 'all-failed'};
            }""", [token, KEY_NAME])
        print("POST /pats ->", json.dumps(created)[:1500])

        ctx.storage_state(path=STATE)
        ctx.close(); browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
