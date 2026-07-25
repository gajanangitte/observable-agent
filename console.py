"""A dependency-free, read-only status console for the self-healing agent.

Most reliability projects ship a web UI on a framework (Next.js, React, Streamlit)
and a pile of npm/pip dependencies. This console makes the same information
glanceable in a browser with **zero new dependencies**: it is built entirely on
the Python standard library (``http.server`` + ``json`` + ``html``), so it adds
nothing to install and preserves the project's "no cloud, no keys, no bill" story.
SigNoz remains the deep observability surface; this is the at-a-glance operator
panel that sits next to it and answers "what has the healer learned, what is armed,
and is the audit trail intact?" without opening SigNoz.

It is strictly READ-ONLY: it renders on-disk runtime state and never mutates
anything (no actions, no fault injection, no config writes), so exposing it is
safe. It surfaces:

  * the running build (``version`` -> service.version, commit, branch, dirty),
  * the live control plane and which named chaos faults are armed (``chaos``),
  * verified remediation memory (what the healer has learned, and how many times
    each fix was proven),
  * the tamper-evident audit ledger: head, record count, and a live integrity
    check (``heal_ledger.verify_chain``),
  * the MCP Contract Lab's last grade + drift fingerprint, and the WattTrace /
    AccessTrace last verdicts, when those reports are present.

The design splits a PURE core from the transport, exactly like the codebase's
other modules (mcp2_model vs mcp2_probe): :func:`snapshot` gathers state into a
plain dict and :func:`render_html` / :func:`render_json` turn it into bytes, with
no sockets involved -- so both are unit-tested offline. Only :func:`serve` and the
handler touch the network.

    python console.py               # serve on http://127.0.0.1:8033
    python console.py --port 9000 --once   # render once to stdout and exit
"""
import argparse
import html
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = int(os.getenv("CONSOLE_PORT", "8033"))


def _read_json(name, default=None):
    """Load a JSON file from the repo root, returning ``default`` if it is absent
    or unreadable. Never raises -- a missing report just renders as 'not present'."""
    try:
        with open(os.path.join(HERE, name)) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return default


def _memory_view():
    """The verified-memory records, flattened to a small display list."""
    recs = _read_json("heal_memory.json", {}) or {}
    out = []
    for rec in recs.values():
        out.append({
            "class_id": rec.get("class_id", ""),
            "slo": rec.get("slo", ""),
            "action": rec.get("action_base", ""),
            "proven": rec.get("count", 0),
            "proven_severity": rec.get("proven_severity", ""),
            "mttr_ms_best": rec.get("mttr_ms_best"),
            "trace_id": rec.get("trace_id", ""),
        })
    out.sort(key=lambda r: r["proven"], reverse=True)
    return out


_STATUS_RANK = {"BREACH": 3, "UNKNOWN": 2, "PASS": 1}


def _worst_cohort(report):
    """The most newsworthy cohort in a WattTrace/AccessTrace report (BREACH beats
    UNKNOWN beats PASS). Both reports keep their verdicts under ``cohorts`` rather
    than at the top level, so an operator sees the worst live verdict at a glance."""
    cohorts = (report or {}).get("cohorts") or []
    if not cohorts:
        return None
    return max(cohorts, key=lambda c: _STATUS_RANK.get(c.get("status"), 0))


def _watt_view(watt):
    c = _worst_cohort(watt)
    if c is None:
        return None
    return {"status": c.get("status"),
            "cohort": c.get("name"),
            "joules_per_answer": c.get("joules_per_verified_answer"),
            "grams_per_answer": c.get("gco2_per_verified_answer")}


def _access_view(access):
    c = _worst_cohort(access)
    if c is None:
        return None
    return {"status": c.get("status"),
            "cohort": c.get("name"),
            "weighted_score": c.get("weighted_score")}


def snapshot():
    """Gather all on-disk state into one JSON-serialisable dict. PURE (no sockets).

    Every section degrades gracefully: a missing file becomes ``None`` or an empty
    list, never an error, so the console renders on a fresh clone too.
    """
    import version
    import chaos
    import heal_ledger

    # Control plane + which named faults are armed. Read the state file directly
    # (do not import Controls, to keep this read-only and side-effect free).
    state = _read_json("heal_state.json", {}) or {}

    class _RO:
        pass
    _ro = _RO()
    _ro.state = state
    try:
        armed = chaos.armed(_ro)
    except Exception:  # noqa: BLE001
        armed = []

    ledger_ok, ledger_msg = heal_ledger.verify_chain(heal_ledger.PATH)
    ledger_records = heal_ledger.records(heal_ledger.PATH)
    ledger_head = heal_ledger.head(heal_ledger.PATH)

    mcp2 = _read_json("mcp2_report.json")
    watt = _read_json("watt_report.json")
    access = _read_json("access_report.json")

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": version.deployment_attributes(),
        "control_plane": state,
        "chaos_armed": armed,
        "faults_catalog": chaos.list_faults(),
        "memory": _memory_view(),
        "ledger": {
            "intact": ledger_ok,
            "message": ledger_msg,
            "records": len(ledger_records),
            "head_event": (ledger_head or {}).get("event"),
            "head_hash": (ledger_head or {}).get("hash", "")[:16],
        },
        "mcp2": None if not mcp2 else {
            "grade": mcp2.get("grade"),
            "fingerprint": mcp2.get("fingerprint"),
            "fault_injected": mcp2.get("fault_injected"),
            "captured_at": mcp2.get("captured_at"),
        },
        "watttrace": _watt_view(watt),
        "accesstrace": _access_view(access),
    }


def render_json(snap=None):
    """The snapshot as pretty JSON bytes (the ``/api/status`` body). PURE."""
    return json.dumps(snap if snap is not None else snapshot(),
                      indent=2).encode("utf-8")


def _pill(text, ok=None):
    """A coloured status pill. ``ok`` True=green, False=red, None=neutral."""
    cls = "pill" + ("" if ok is None else (" ok" if ok else " bad"))
    return f'<span class="{cls}">{html.escape(str(text))}</span>'


def _rows(headers, rows):
    if not rows:
        return '<p class="muted">none yet</p>'
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = ""
    for r in rows:
        body += "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(snap=None):
    """The full console page as HTML bytes. PURE (no sockets); every value is
    HTML-escaped so a state file can never inject markup."""
    s = snap if snap is not None else snapshot()
    v = s["version"]
    led = s["ledger"]

    ver_line = (f'{v.get("service.version", "?")}'
                + (f' · {v["deployment.branch"]}' if v.get("deployment.branch") else "")
                + (' · dirty' if v.get("deployment.dirty") else ""))

    armed = s["chaos_armed"]
    armed_html = (", ".join(_pill(a, ok=False) for a in armed)
                  if armed else _pill("none, healthy", ok=True))

    mem_rows = [(m["slo"], m["action"], f'{m["proven"]}×', m["proven_severity"],
                 (f'{m["mttr_ms_best"] / 1000:.0f}s' if m["mttr_ms_best"] else "n/a"),
                 m["trace_id"][:12]) for m in s["memory"]]

    cp = s["control_plane"]
    cp_rows = [(k, str(cp[k])) for k in sorted(cp)] if cp else []

    def section(title, inner):
        return f'<section><h2>{html.escape(title)}</h2>{inner}</section>'

    mcp2 = s.get("mcp2")
    mcp2_html = (_pill("no report", ok=None) if not mcp2 else
                 (_pill(mcp2.get("grade", "?"), ok=(mcp2.get("grade") == "CERTIFIED"))
                  + f' · fingerprint <code>{html.escape(str(mcp2.get("fingerprint")))}</code>'))

    watt = s.get("watttrace")
    watt_html = (_pill("no report", ok=None) if not watt else
                 (_pill(watt.get("status", "?"), ok=(watt.get("status") == "PASS"))
                  + (f' · {html.escape(str(watt.get("joules_per_answer")))} J/answer'
                     if watt.get("joules_per_answer") is not None else "")))

    access = s.get("accesstrace")
    access_html = (_pill("no report", ok=None) if not access else
                   (_pill(access.get("status", "?"), ok=(access.get("status") == "PASS"))
                    + (f' · weighted debt {html.escape(str(access.get("weighted_score")))}'
                       if access.get("weighted_score") is not None else "")))

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Self-Healing Agent status console</title>
<style>
 :root {{ color-scheme: dark; }}
 body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0; background:#0d1117; color:#e6edf3; }}
 header {{ padding: 20px 28px; border-bottom: 1px solid #21262d; }}
 header h1 {{ margin: 0 0 4px; font-size: 20px; }}
 header .sub {{ color:#8b949e; font-size: 13px; }}
 main {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap:18px; padding: 24px 28px; }}
 section {{ background:#161b22; border:1px solid #21262d; border-radius:10px; padding:16px 18px; }}
 h2 {{ margin:0 0 12px; font-size:14px; text-transform:uppercase; letter-spacing:.04em; color:#8b949e; }}
 table {{ width:100%; border-collapse:collapse; font-size:13px; }}
 th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid #21262d; }}
 th {{ color:#8b949e; font-weight:600; }}
 code {{ background:#0d1117; padding:1px 5px; border-radius:4px; font-size:12px; }}
 .pill {{ display:inline-block; padding:2px 10px; border-radius:20px; font-size:12px; font-weight:600;
          background:#30363d; color:#e6edf3; }}
 .pill.ok {{ background:#1a7f37; }}
 .pill.bad {{ background:#a4272a; }}
 .muted {{ color:#8b949e; font-size:13px; }}
 .big {{ font-size:26px; font-weight:700; }}
 footer {{ padding: 12px 28px 28px; color:#8b949e; font-size:12px; }}
 a {{ color:#58a6ff; }}
</style></head>
<body>
<header>
  <h1>Self-Healing SRE Sidekick status console</h1>
  <div class="sub">read-only · zero dependencies · build <code>{html.escape(ver_line)}</code>
   · generated {html.escape(s["generated_at"])} · <a href="/api/status">/api/status</a></div>
</header>
<main>
  {section("Audit ledger (tamper-evident)",
           f'<p class="big">{_pill("INTACT" if led["intact"] else "TAMPERED", ok=led["intact"])}</p>'
           f'<p class="muted">{html.escape(led["message"])}</p>'
           f'<p class="muted">head: <code>{html.escape(str(led["head_event"]))}</code> '
           f'@ <code>{html.escape(led["head_hash"])}</code></p>')}
  {section("Chaos: armed faults", f'<p>{armed_html}</p>'
           f'<p class="muted">catalog: {", ".join(html.escape(f) for f in s["faults_catalog"])}</p>')}
  {section("Verified remediation memory",
           _rows(["SLO", "fix", "proven", "severity", "best MTTR", "trace"], mem_rows))}
  {section("Control plane", _rows(["knob", "value"], cp_rows))}
  {section("MCP Contract Lab (Track 02)", f'<p>{mcp2_html}</p>')}
  {section("WattTrace GreenOps (Track 03)", f'<p>{watt_html}</p>')}
  {section("AccessTrace WCAG (Track 03)", f'<p>{access_html}</p>')}
</main>
<footer>SigNoz is the deep observability surface; this console is the at-a-glance operator panel next to it.</footer>
</body></html>"""
    return page.encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(render_html(), "text/html; charset=utf-8")
        elif path in ("/api/status", "/status.json"):
            self._send(render_json(), "application/json; charset=utf-8")
        elif path == "/healthz":
            self._send(b'{"ok":true}', "application/json; charset=utf-8")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        return  # keep the console quiet; SigNoz has the real logs


def serve(port=DEFAULT_PORT, host="127.0.0.1"):
    server = ThreadingHTTPServer((host, port), _Handler)
    print(f"status console on http://{host}:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only status console for the self-healer.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--once", action="store_true",
                    help="render the page to stdout once and exit (no server)")
    ap.add_argument("--json", action="store_true", help="with --once, emit JSON not HTML")
    args = ap.parse_args(argv)
    if args.once:
        sys.stdout.buffer.write(render_json() if args.json else render_html())
        return 0
    serve(port=args.port, host=args.host)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
