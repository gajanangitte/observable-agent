"""Log into self-hosted SigNoz via the UI, capture the auth token + login API
path, and take a landing screenshot. Learns the endpoint the SPA actually uses.
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
EMAIL = os.environ.get("SIGNOZ_EMAIL", "admin@signoz.local")
PASSWORD = os.environ.get("SIGNOZ_PASSWORD", "changeme")
SHOTS = "blog/shots"

posts = []


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()

        def on_response(resp):
            try:
                if resp.request.method in ("POST", "GET") and "/api/" in resp.url:
                    posts.append((resp.request.method, resp.url, resp.status))
            except Exception:
                pass

        page.on("response", on_response)

        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.screenshot(path=f"{SHOTS}/00_landing.png")
        print("URL after load:", page.url)
        print("title:", page.title())

        # Fill email
        try:
            email_in = page.locator("input[type='email'], input[name='email'], input#email").first
            email_in.wait_for(timeout=8000)
            email_in.fill(EMAIL)
            print("filled email")
        except Exception as e:
            print("no email field:", e)

        # Password may already be visible, or behind a Next button
        pw = page.locator("input[type='password']").first
        if pw.count() == 0 or not pw.is_visible():
            for label in ("Next", "Continue", "Login", "Log in"):
                b = page.get_by_role("button", name=label)
                if b.count() > 0:
                    try:
                        b.first.click(timeout=3000)
                        print("clicked", label)
                        break
                    except Exception:
                        pass
        page.wait_for_timeout(1500)
        try:
            pw = page.locator("input[type='password']").first
            pw.wait_for(timeout=8000)
            pw.fill(PASSWORD)
            print("filled password")
        except Exception as e:
            print("no password field:", e)

        for label in ("Login", "Log in", "Sign in", "Next", "Continue"):
            b = page.get_by_role("button", name=label)
            if b.count() > 0:
                try:
                    b.first.click(timeout=3000)
                    print("submitted via", label)
                    break
                except Exception:
                    pass

        page.wait_for_timeout(6000)
        print("URL after login:", page.url)
        page.screenshot(path=f"{SHOTS}/01_home.png", full_page=False)

        ls = page.evaluate("() => JSON.stringify(window.localStorage)")
        try:
            data = json.loads(ls)
            keys = list(data.keys())
            print("localStorage keys:", keys)
            for k, v in data.items():
                if any(t in k.lower() for t in ("token", "auth", "jwt", "user")):
                    print(f"  {k} = {str(v)[:80]}")
        except Exception as e:
            print("ls parse err:", e)

        print("=== API calls seen ===")
        for m, u, s in posts:
            if any(t in u.lower() for t in ("login", "register", "precheck", "user", "jwt", "token")):
                print(f"  {m} {s} {u}")

        # Persist session so follow-up scripts skip the login flow.
        ctx.storage_state(path="blog/state.json")
        try:
            token = page.evaluate("() => window.localStorage.getItem('AUTH_TOKEN')")
            with open("blog/token.txt", "w") as fh:
                fh.write(token or "")
            print("saved blog/state.json and blog/token.txt")
        except Exception as e:
            print("token save err:", e)

        ctx.close()
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
