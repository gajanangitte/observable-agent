"""Ensure the SigNoz alerts around the MCP Contract Lab exist (reproducibly).

Every certification run emits one ERROR-flagged ``cert.<contract>`` span per contract
that did not PASS, tagged with ``mcp2.status = BREACH | UNKNOWN``. Two alerts watch
those spans, so the same verdict the certification grades on is the signal that pages:

  * Contract breach   -- fires when any span with ``mcp2.status = BREACH`` appears in
                         the window: the MCP server violated a reliability contract (a
                         malformed result, an unhandled bad argument, a latency SLO
                         miss, catalog drift, ...). CRITICAL: a certified integration
                         just regressed.
  * Coverage blind spot -- fires when any span with ``mcp2.status = UNKNOWN`` appears:
                         the lab could NOT evaluate a contract (no safe read sample,
                         handshake failure, ...). WARNING, notify-only: a green run with
                         blind spots is not a trustworthy green run. Fail closed.

Both thresholds are 0 (any occurrence fires), because a reliability contract is
pass/fail, not a rate. The lab's own CLI already exits non-zero on a FAILED/BLIND
grade for CI gating; these alerts are the always-on SigNoz view of the same truth.

  python mcp2_alert.py --ensure                 # create/update both alerts
  python mcp2_alert.py --status                 # show every rule's live state
  python mcp2_alert.py --ensure --window 5m --channel local-webhook

Auth: a SigNoz API key in ``.signoz_api_key`` (the same file the dashboards use).
"""
import argparse
import os
import sys
import json
import urllib.error
import urllib.request
from pathlib import Path

# The service and the mcp2.status span attribute are the contract between the lab
# and these alerts (the same attribute the cert.<contract> spans carry).
CERT_SERVICE = "mcp-contract-lab"

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.getenv("SIGNOZ_UI", "http://localhost:8080").rstrip("/")
# Read lazily-tolerant: the alert SPECS need no key (only the HTTP calls do), so a
# fresh clone without .signoz_api_key can still import this module and unit-test the
# spec shapes. The key is required only when actually talking to SigNoz.
_KEYFILE = Path(HERE) / ".signoz_api_key"
KEY = _KEYFILE.read_text().strip() if _KEYFILE.exists() else ""

# Stable identities: keep the names fixed so --ensure updates in place.
BREACH_ALERT = "MCP Contract Breach: a reliability contract was violated"
BLIND_ALERT = "MCP Coverage Blind Spot: a contract could not be evaluated"


def _bq(name, filter_expr, disabled=False):
    """A v5 traces builder query that counts cert spans matching a filter."""
    return {"type": "builder_query", "spec": {
        "name": name, "signal": "traces", "disabled": disabled,
        "stepInterval": "1m", "aggregations": [{"expression": "count()"}],
        "filter": {"expression": filter_expr}}}


def _spec(name, status, severity, role, summary, description, window, channel):
    filt = f"service.name = '{CERT_SERVICE}' AND mcp2.status = '{status}'"
    return {
        "alert": name,
        "alertType": "TRACES_BASED_ALERT",
        "ruleType": "threshold_rule",
        "version": "v5",
        "evalWindow": window,
        "frequency": "1m",
        "condition": {
            "compositeQuery": {
                "queryType": "builder",
                "panelType": "graph",
                "queries": [_bq("A", filt)],
            },
            "target": 0,
            "op": "above",
            "matchType": "at_least_once",
            "selectedQueryName": "A",
        },
        "preferredChannels": [channel] if channel else [],
        "labels": {"severity": severity, "slo": "mcp_contract", "cert_role": role},
        "annotations": {"summary": summary, "description": description},
    }


def breach_spec(window, channel):
    """Any BREACH verdict in the window pages: a certified MCP integration regressed."""
    return _spec(
        BREACH_ALERT, "BREACH", "critical", "trigger",
        summary="An MCP reliability contract breached",
        description=("A cert span was ERROR-flagged with mcp2.status=BREACH: the MCP "
                     "server violated a reliability contract (malformed result, "
                     "unhandled bad argument, latency SLO miss, catalog drift, or a "
                     "broken handshake). A previously certified integration just "
                     "regressed."),
        window=window, channel=channel)


def blind_spec(window, channel):
    """Any UNKNOWN verdict in the window warns: the run had a coverage blind spot."""
    return _spec(
        BLIND_ALERT, "UNKNOWN", "warning", "notify",
        summary="An MCP contract could not be evaluated (blind spot)",
        description=("A cert span carried mcp2.status=UNKNOWN: the lab could not "
                     "evaluate a contract (no safe read sample, a handshake failure, "
                     "...). Fail closed: a green run that skipped a check is not a "
                     "trustworthy green run. Fix the probe or credentials, do not "
                     "ignore it."),
        window=window, channel=channel)


ALERT_SPECS = [breach_spec, blind_spec]


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"SIGNOZ-API-KEY": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def rules():
    _st, body = _req("GET", "/api/v1/rules")
    d = json.loads(body)
    data = d.get("data", d)
    rs = data.get("rules", data) if isinstance(data, dict) else data
    return rs if isinstance(rs, list) else []


def find(name):
    for r in rules():
        if (r.get("alert") or r.get("alertName")) == name:
            return r
    return None


def ensure(window, channel):
    ok = True
    for spec_fn in ALERT_SPECS:
        body = spec_fn(window, channel)
        name = body["alert"]
        existing = find(name)
        if existing:
            rid = existing["id"]
            st, resp = _req("PUT", f"/api/v1/rules/{rid}", body)
            print(f"UPDATE {name!r} (id={rid}) -> HTTP {st}")
        else:
            st, resp = _req("POST", "/api/v1/rules", body)
            print(f"CREATE {name!r} -> HTTP {st}")
        if st >= 300:
            print("  " + resp[:300])
            ok = False
    return ok


def status():
    for r in rules():
        if (r.get("alert") or r.get("alertName")) in (BREACH_ALERT, BLIND_ALERT):
            print(f"  {(r.get('state') or '?'):10s}  {r.get('alert') or r.get('alertName')}  "
                  f"(id={r.get('id')}, win={r.get('evalWindow')}, chans={r.get('preferredChannels')})")


def main():
    ap = argparse.ArgumentParser(description="Ensure the MCP Contract Lab's SigNoz alerts")
    ap.add_argument("--ensure", action="store_true", help="create or update both alerts")
    ap.add_argument("--status", action="store_true", help="print every rule's live state")
    ap.add_argument("--window", default=os.getenv("MCP2_ALERT_WINDOW", "5m"),
                    help="eval window (short = resolves quickly after a clean run; default 5m)")
    ap.add_argument("--channel", default=os.getenv("MCP2_ALERT_CHANNEL", "local-webhook"),
                    help="notification channel")
    args = ap.parse_args()
    ok = True
    if args.ensure:
        ok = ensure(args.window, args.channel)
    if args.status or not args.ensure:
        status()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
