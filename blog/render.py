"""Render blog.md -> blog.html with a clean, readable style. Images resolve
because blog.html lives next to the shots/ folder."""
import re
import markdown

src = open("blog.md", encoding="utf-8").read()
# strip YAML front-matter
if src.startswith("---"):
    src = src.split("---", 2)[2]

# pull the front-matter title for the <title> tag
m = re.search(r'title:\s*"([^"]+)"', open("blog.md", encoding="utf-8").read())
title = m.group(1) if m else "Blog"

html_body = markdown.markdown(
    src, extensions=["tables", "fenced_code", "toc", "sane_lists"]
)

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin:0; background:#f6f7f9; color:#1f2328;
    font:17px/1.7 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:760px; margin:0 auto; padding:48px 24px 96px; }}
  h1 {{ font-size:2.1rem; line-height:1.2; margin:.2em 0 .4em; letter-spacing:-.02em; }}
  h2 {{ font-size:1.5rem; margin:2.2em 0 .6em; letter-spacing:-.01em;
    padding-top:.4em; border-top:1px solid #e6e8eb; }}
  h3 {{ font-size:1.15rem; margin:1.8em 0 .5em; }}
  p,li {{ color:#30363d; }}
  a {{ color:#0969da; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
  blockquote {{ margin:1.4em 0; padding:.6em 1.1em; border-left:4px solid #d0a215;
    background:#fbf8ec; border-radius:6px; color:#4b4636; font-style:italic; }}
  code {{ font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:.86em;
    background:#eff1f3; padding:.15em .4em; border-radius:5px; }}
  pre {{ background:#0d1117; color:#e6edf3; padding:16px 18px; border-radius:10px;
    overflow:auto; font-size:.82rem; line-height:1.55; }}
  pre code {{ background:none; padding:0; color:inherit; }}
  img {{ max-width:100%; height:auto; border:1px solid #d0d7de; border-radius:10px;
    box-shadow:0 6px 24px rgba(31,35,40,.10); margin:1.2em 0; display:block; }}
  table {{ border-collapse:collapse; width:100%; margin:1.2em 0; font-size:.92rem; }}
  th,td {{ border:1px solid #d0d7de; padding:8px 12px; text-align:left; }}
  th {{ background:#eff1f3; }}
  hr {{ border:none; border-top:1px solid #e6e8eb; margin:2.4em 0; }}
  .meta {{ color:#8b949e; font-size:.85rem; margin-bottom:2.2em; }}
</style></head>
<body><div class="wrap">
<div class="meta">Agents of SigNoz hackathon · Early-Win blog · preview</div>
__BODY__
</div></body></html>"""

out = TEMPLATE.replace("__TITLE__", title).replace("__BODY__", html_body)
open("blog.html", "w", encoding="utf-8").write(out)
print("wrote blog.html")
