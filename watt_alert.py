"""Ensure the SigNoz alerts around WattTrace GreenOps exist (reproducibly).

Every WattTrace run emits a watt.cohort span carrying the GreenOps verdict
(watttrace.status = PASS / BREACH / UNKNOWN); a BREACH span is also ERROR-flagged.
Every llm.chat call whose work was thrown away is tagged
watttrace.energy.disposition = wasted. Two alerts watch those spans, so the same
verdict the GreenOps gate grades on is the signal that pages:

  * Energy budget breach -- fires when any watt.cohort span with
                            watttrace.status = BREACH appears in the window: a cohort
                            burned more energy per verified answer than the budget
                            allows. CRITICAL: the agent just got more wasteful.
  * Retry energy waste   -- fires when any llm.chat span with
                            watttrace.energy.disposition = wasted appears: energy was
                            spent on a dropped retry or a failed answer. WARNING,
                            notify-only: the tax is climbing even if the budget still
                            holds.

Both thresholds are 0 (any occurrence fires). The budget breach is a pass/fail
verdict, not a rate, and any wasted joule is worth surfacing. The suite's own CLI
already exits non-zero on a BREACH with --gate for CI gating; these alerts are the
always-on SigNoz view of the same truth.

  python watt_alert.py --ensure                 # create/update both alerts
  python watt_alert.py --status                 # show every rule's live state
  python watt_alert.py --ensure --window 5m --channel local-webhook

Auth: a SigNoz API key in .signoz_api_key (the same file the dashboards use).
"""
import argparse
import os
import sys
import json
import urllib.error
import urllib.request
from pathlib import Path

# The service and the watttrace.* span attributes are the contract between the
# WattTrace suite and these alerts (the same attributes watt_report.py emits).
WATT_SERVICE = "watttrace"

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.getenv("SIGNOZ_UI", "http://localhost:8080").rstrip("/")
# Read lazily-tolerant: the alert SPECS need no key (only the HTTP calls do), so a
# fresh clone without .signoz_api_key can still import this module and unit-test the
# spec shapes. The key is required only when actually talking to SigNoz.
_KEYFILE = Path(HERE) / ".signoz_api_key"
KEY = _KEYFILE.read_text().strip() if _KEYFILE.exists() else ""

# Stable identities: keep the names fixed so --ensure updates in place.
BREACH_ALERT = "WattTrace Energy Budget Breach: joules per verified answer exceeded budget"
WASTE_ALERT = "WattTrace Retry Energy Waste: energy spent on retried or failed work"


def _bq(name, filter_expr, disabled=False):
    """A v5 traces builder query that counts watttrace spans matching a filter."""
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
        "labels": {"severity": severity, "slo": slo, "watt_role": role},
        "annotations": {"summary": summary, "description": description},
    }


def breach_spec(window, channel):
    """Any BREACH cohort in the window pages: the agent's energy per answer regressed."""
    filt = (f"service.name = '{WATT_SERVICE}' AND name = 'watt.cohort' "
            f"AND watttrace.status = 'BREACH'")
    return _spec(
        BREACH_ALERT, filt, "critical", "trigger", "watt_energy_budget",
        summary="A WattTrace cohort breached the energy budget",
        description=("A watt.cohort span was ERROR-flagged with watttrace.status=BREACH: "
                     "the agent burned more energy per VERIFIED answer than the GreenOps "
                     "budget allows. Usually a retry or failure regression is spending real "
                     "joules for no extra correct answers. Look at the wasted-energy panel and "
                     "the joules-per-token trend to see what moved."),
        window=window, channel=channel)


def waste_spec(window, channel):
    """Any wasted-energy call in the window warns: the retry tax is being paid."""
    filt = (f"service.name = '{WATT_SERVICE}' AND name = 'llm.chat' "
            f"AND watttrace.energy.disposition = 'wasted'")
    return _spec(
        WASTE_ALERT, filt, "warning", "notify", "watt_retry_waste",
        summary="WattTrace saw energy spent on retried or failed work",
        description=("An llm.chat span carried watttrace.energy.disposition=wasted: real "
                     "energy was spent on a dropped retry or a failed answer that produced "
                     "nothing verifiable. Notify-only: the budget may still hold, but the tax "
                     "is climbing. Chase the fault before it breaches."),
        window=window, channel=channel)


ALERT_SPECS = [breach_spec, waste_spec]


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
        if (r.get("alert") or r.get("alertName")) in (BREACH_ALERT, WASTE_ALERT):
            print(f"  {(r.get('state') or '?'):10s}  {r.get('alert') or r.get('alertName')}  "
                  f"(id={r.get('id')}, win={r.get('evalWindow')}, chans={r.get('preferredChannels')})")


def main():
    ap = argparse.ArgumentParser(description="Ensure the WattTrace GreenOps SigNoz alerts")
    ap.add_argument("--ensure", action="store_true", help="create or update both alerts")
    ap.add_argument("--status", action="store_true", help="print every rule's live state")
    ap.add_argument("--window", default=os.getenv("WATT_ALERT_WINDOW", "5m"),
                    help="eval window (short = resolves quickly after a clean run; default 5m)")
    ap.add_argument("--channel", default=os.getenv("WATT_ALERT_CHANNEL", "local-webhook"),
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
