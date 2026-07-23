"""Ensure the SigNoz alerts around AccessTrace WCAG journeys exist (reproducibly).

Every AccessTrace run emits access.journey spans carrying the WCAG verdict
(a11y.status = PASS / BREACH / UNKNOWN); a BREACH span is also ERROR-flagged, and
so is each access.step span for the stage that broke. Two alerts watch those spans,
so the same verdict the CI accessibility gate grades on is the signal that pages:

  * WCAG budget breach   -- fires when any access.journey span with a11y.status =
                            BREACH appears in the window: the product broke a
                            critical or serious WCAG rule (or piled up too much
                            weighted debt). CRITICAL: accessibility just regressed.
  * Breaching stage      -- fires when any access.step span with a11y.status =
                            BREACH appears: it tells you WHICH stage of the user
                            flow (navigation, main content, form) fell apart.
                            WARNING, notify-only: the localiser for the page above.

Both thresholds are 0 (any occurrence fires): a WCAG budget breach is a pass/fail
verdict, not a rate. The suite's own CLI already exits non-zero on a BREACH with
--gate for CI gating; these alerts are the always-on SigNoz view of the same truth.

  python access_alert.py --ensure                 # create/update both alerts
  python access_alert.py --status                 # show every rule's live state
  python access_alert.py --ensure --window 5m --channel local-webhook

Auth: a SigNoz API key in .signoz_api_key (the same file the dashboards use).
"""
import argparse
import os
import sys
import json
import urllib.error
import urllib.request
from pathlib import Path

# The service and the a11y.* span attributes are the contract between the
# AccessTrace suite and these alerts (the same attributes access_report.py emits).
ACCESS_SERVICE = "accesstrace"

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.getenv("SIGNOZ_UI", "http://localhost:8080").rstrip("/")
# The alert SPECS need no key (only the HTTP calls do), so a fresh clone without
# .signoz_api_key can still import this module and unit-test the spec shapes.
_KEYFILE = Path(HERE) / ".signoz_api_key"
KEY = _KEYFILE.read_text().strip() if _KEYFILE.exists() else ""

# Stable identities: keep the names fixed so --ensure updates in place.
BREACH_ALERT = "AccessTrace WCAG Budget Breach: a journey broke a critical or serious rule"
STAGE_ALERT = "AccessTrace Breaching Stage: which step of the user flow fails WCAG"


def _bq(name, filter_expr, disabled=False):
    """A v5 traces builder query that counts accesstrace spans matching a filter."""
    return {"type": "builder_query", "spec": {
        "name": name, "signal": "traces", "disabled": disabled,
        "stepInterval": "1m", "aggregations": [{"expression": "count()"}],
        "filter": {"expression": filter_expr}}}


def _spec(name, filt, severity, role, slo, summary, description, window, channel):
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
        "labels": {"severity": severity, "slo": slo, "access_role": role},
        "annotations": {"summary": summary, "description": description},
    }


def breach_spec(window, channel):
    """Any BREACH journey in the window pages: the product's WCAG health regressed."""
    filt = (f"service.name = '{ACCESS_SERVICE}' AND name = 'access.journey' "
            f"AND a11y.status = 'BREACH'")
    return _spec(
        BREACH_ALERT, filt, "critical", "trigger", "access_wcag_budget",
        summary="An AccessTrace journey breached the WCAG budget",
        description=("An access.journey span was ERROR-flagged with a11y.status=BREACH: the "
                     "product broke a critical or serious WCAG rule (for example a control or "
                     "image with no accessible name, or failing colour contrast), or piled up "
                     "more weighted accessibility debt than the budget allows. A keyboard or "
                     "screen-reader user just lost part of the product. Open the AccessTrace "
                     "dashboard and drill into the breaching-stage panel to see where."),
        window=window, channel=channel)


def stage_spec(window, channel):
    """Any breaching step localises the failure to a stage of the user flow."""
    filt = (f"service.name = '{ACCESS_SERVICE}' AND name = 'access.step' "
            f"AND a11y.status = 'BREACH'")
    return _spec(
        STAGE_ALERT, filt, "warning", "notify", "access_stage_breach",
        summary="AccessTrace saw a specific journey stage fail WCAG",
        description=("An access.step span carried a11y.status=BREACH: one stage of the user "
                     "journey (the navigation, the main content, or the form) broke a WCAG rule. "
                     "Notify-only: this is the localiser for the budget-breach page above, so you "
                     "know exactly which part of the flow to fix first."),
        window=window, channel=channel)


ALERT_SPECS = [breach_spec, stage_spec]


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
        if (r.get("alert") or r.get("alertName")) in (BREACH_ALERT, STAGE_ALERT):
            print(f"  {(r.get('state') or '?'):10s}  {r.get('alert') or r.get('alertName')}  "
                  f"(id={r.get('id')}, win={r.get('evalWindow')}, chans={r.get('preferredChannels')})")


def main():
    ap = argparse.ArgumentParser(description="Ensure the AccessTrace WCAG SigNoz alerts")
    ap.add_argument("--ensure", action="store_true", help="create or update both alerts")
    ap.add_argument("--status", action="store_true", help="print every rule's live state")
    ap.add_argument("--window", default=os.getenv("ACCESS_ALERT_WINDOW", "5m"),
                    help="eval window (short = resolves quickly after a clean run; default 5m)")
    ap.add_argument("--channel", default=os.getenv("ACCESS_ALERT_CHANNEL", "local-webhook"),
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
