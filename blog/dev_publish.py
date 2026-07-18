"""Publish the hackathon blogs to Dev.to via the Forem API.

Dev.to's public API can't upload images, so image URLs must be public. We host
them in the GitHub submission repo and reference raw.githubusercontent.com URLs.

Env:
  DEVTO_API_KEY   required. dev.to -> Settings -> Extensions -> "DEV Community API Keys".
  IMAGE_BASE      required. e.g. https://raw.githubusercontent.com/<user>/<repo>/main/blog/
  DEVTO_PUBLISH   "1" to publish live; default drafts (published:false).

Usage:
  python dev_publish.py            # create/update all 3 as drafts, cross-linked
  DEVTO_PUBLISH=1 python dev_publish.py   # flip them live

State (article ids/urls) is saved to devto_articles.json so re-runs UPDATE
instead of creating duplicates.
"""
import json
import os
import re
import sys
import time
import urllib.request

API = "https://dev.to/api/articles"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "devto_articles.json")
SERIES = "Agents of SigNoz"

# key -> (markdown file, cover image relative path, tags[max 4])
BLOGS = {
    "eyes": ("blog.md", "shots/02_trace_waterfall.png",
             ["observability", "opentelemetry", "signoz", "llm"]),
    "retrytax": ("blog2.md", "shots2/01_chaos_trace.png",
                 ["observability", "opentelemetry", "signoz", "llm"]),
    "mcp": ("blog3.md", "shots3/01_introspect_trace.png",
            ["observability", "opentelemetry", "signoz", "mcp"]),
}
# order matters: earlier posts must exist before later posts link to them.
ORDER = ["eyes", "retrytax", "mcp"]


def _req(method, url, key, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("api-key", key)
    r.add_header("Content-Type", "application/json")
    r.add_header("Accept", "application/vnd.forem.api-v1+json")
    r.add_header("User-Agent",
                 "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read().decode())


def parse_front_matter(text):
    """Split '--- ... ---' YAML front matter from the markdown body."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not m:
        return {}, text
    fm_raw, body = m.group(1), m.group(2)
    fm = {}
    for line in fm_raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip().strip('"')
    return fm, body


def rewrite_images(body, image_base):
    """shots/x.png -> <image_base>shots/x.png for every markdown image."""
    return re.sub(r"\]\((shots\d?/)", lambda m: "](" + image_base + m.group(1), body)


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(s):
    with open(STATE, "w") as f:
        json.dump(s, f, indent=2)


def build_payload(key_name, image_base, publish, links):
    fname, cover, tags = BLOGS[key_name]
    with open(os.path.join(HERE, fname), encoding="utf-8") as f:
        fm, body = parse_front_matter(f.read())
    body = rewrite_images(body, image_base)
    # Replace placeholder cross-links [text](#) with real published URLs.
    for placeholder_key, url in links.items():
        if url:
            body = body.replace(f"]({placeholder_key})", f"]({url})")
    article = {
        "title": fm.get("title", key_name),
        "body_markdown": body,
        "published": publish,
        "tags": tags[:4],
        "series": SERIES,
        "main_image": image_base + cover,
    }
    if fm.get("description"):
        desc = fm["description"]
        if len(desc) > 150:
            desc = desc[:147].rsplit(" ", 1)[0] + "..."
        article["description"] = desc
    if fm.get("canonical_url"):
        article["canonical_url"] = fm["canonical_url"]
    return {"article": article}


def main():
    key = os.getenv("DEVTO_API_KEY")
    image_base = os.getenv("IMAGE_BASE")
    publish = os.getenv("DEVTO_PUBLISH") == "1"
    if not key or not image_base:
        sys.exit("set DEVTO_API_KEY and IMAGE_BASE")
    if not image_base.endswith("/"):
        image_base += "/"

    state = load_state()

    # First pass so URLs exist; second pass injects cross-links + publishes.
    for _pass in (1, 2):
        # Map the named placeholder tokens to real URLs once known.
        # blog2/blog3 link to blog1 (eyes); blog3 also links to blog2 (retrytax).
        links = {"BLOG_EYES": state.get("eyes", {}).get("url", ""),
                 "BLOG_RETRYTAX": state.get("retrytax", {}).get("url", "")}
        for name in ORDER:
            payload = build_payload(name, image_base, publish, links)
            rec = state.get(name)
            try:
                if rec and rec.get("id"):
                    res = _req("PUT", f"{API}/{rec['id']}", key, payload)
                    action = "updated"
                else:
                    res = _req("POST", API, key, payload)
                    action = "created"
            except urllib.error.HTTPError as e:  # noqa
                print(f"  {name}: HTTP {e.code} {e.read().decode()[:300]}")
                continue
            state[name] = {"id": res["id"], "url": res["url"],
                           "slug": res.get("slug"), "published": res.get("published")}
            save_state(state)
            print(f"  {name}: {action} -> {res['url']} (published={res.get('published')})")
            time.sleep(3)  # be gentle with the rate limit
        if all(state.get(n, {}).get("url") for n in ORDER):
            continue

    print("\nDone. State in devto_articles.json")
    for n in ORDER:
        if state.get(n):
            print(f"  {n}: {state[n]['url']}")


if __name__ == "__main__":
    main()
