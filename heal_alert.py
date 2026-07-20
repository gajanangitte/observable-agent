"""Ensure the SigNoz alerts around the self-healing loop exist (reproducibly).

The loop is driven by REAL SigNoz threshold alerts, and each alert's threshold is
the SAME SLO the healer's sensors enforce (imported from ``heal_sensors``), so the
alert that pages and the detector that acts can never drift apart:

  * Retry tax     -- fires when the retried FRACTION of ``llm.chat`` calls on the
                     managed workload exceeds the 5% SLO (a ratio, not "any retry").
  * Cost runaway  -- fires when ``llm.chat`` calls per ``agent.invoke`` request
                     exceed the 6-calls/request SLO (bill-shock).
  * Heal backstop -- fires when an ``agent.heal`` cycle ends with
                     ``heal.healed = false`` (the autonomous loop could not close;
                     page a human). This one is NOTIFY-ONLY: the bridge must never
                     turn it into another heal (labelled ``heal_role=notify``).

``heal_bridge.py`` turns a firing TRIGGER alert into a governed heal; once the
workload is fixed the same alert resolves. This script creates or updates every
rule idempotently (matched by name) so the demo is reproducible.

  python heal_alert.py --ensure                 # create/update all three alerts
  python heal_alert.py --status                 # show every rule's live state
  python heal_alert.py --ensure --window 5m --channel local-webhook

Auth: a SigNoz API key in ``.signoz_api_key`` (the same file the dashboards use).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Single source of truth: the alert thresholds ARE the healer's SLOs. Fall back to
# the documented defaults if the sensor stack cannot be imported standalone.
try:
    from heal_sensors import (TARGET_SERVICE, RETRY_SLO_MAX_RATE,
                              COST_SLO_MAX_CALLS_PER_REQ)
except Exception:  # noqa: BLE001
    TARGET_SERVICE = "observable-agent"
    RETRY_SLO_MAX_RATE = 0.05
    COST_SLO_MAX_CALLS_PER_REQ = 6

HEALER_SERVICE = "self-healer"

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.getenv("SIGNOZ_UI", "http://localhost:8080").rstrip("/")
# Read lazily-tolerant: the alert SPECS need no key (only the HTTP calls do), so a
# fresh clone without .signoz_api_key can still import this module and unit-test the
# spec shapes. The key is required only when actually talking to SigNoz.
_KEYFILE = Path(HERE) / ".signoz_api_key"
KEY = _KEYFILE.read_text().strip() if _KEYFILE.exists() else ""

# Names are stable identities: keep the retry name as first deployed so --ensure
# updates it in place instead of orphaning the old rule.
RETRY_ALERT = "Agent Retry Tax: LLM responses being retried"
COST_ALERT = "Agent Cost Runaway: LLM calls per request above SLO"
HEAL_ALERT = "Self-Healer backstop: an incident was not auto-resolved"


def _bq(name, filter_expr, disabled=False):
    """A v5 traces builder query that counts spans matching a filter."""
    return {"type": "builder_query", "spec": {
        "name": name, "signal": "traces", "disabled": disabled,
        "stepInterval": "1m", "aggregations": [{"expression": "count()"}],
        "filter": {"expression": filter_expr}}}


def _ratio_spec(name, window, channel, numerator, denominator, target,
                severity, slo_label, summary, description):
    """A threshold alert on the ratio numerator/denominator (a formula query), so
    the trigger is a RATE against an SLO, not a raw count."""
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
                "queries": [
                    _bq("A", numerator, disabled=True),
                    _bq("B", denominator, disabled=True),
                    {"type": "builder_formula", "spec": {"name": "F1", "expression": "A/B"}},
                ],
            },
            "target": target,
            "op": "above",
            "matchType": "at_least_once",
            "selectedQueryName": "F1",
        },
        "preferredChannels": [channel] if channel else [],
        "labels": {"severity": severity, "slo": slo_label, "heal_role": "trigger"},
        "annotations": {"summary": summary, "description": description},
    }


def retry_spec(window, channel):
    """Retry tax: retried fraction of llm.chat above the 5% SLO."""
    return _ratio_spec(
        RETRY_ALERT, window, channel,
        numerator=(f"service.name = '{TARGET_SERVICE}' AND name = 'llm.chat' "
                   "AND retry.reason = 'response_dropped'"),
        denominator=f"service.name = '{TARGET_SERVICE}' AND name = 'llm.chat'",
        target=RETRY_SLO_MAX_RATE, severity="warning", slo_label="retry_tax",
        summary=f"Retry rate above the {RETRY_SLO_MAX_RATE:.0%} SLO",
        description=(f"More than {RETRY_SLO_MAX_RATE:.0%} of llm.chat calls on "
                     f"{TARGET_SERVICE} were dropped and retried (retry.reason="
                     "response_dropped): the agent is burning tokens on responses "
                     "it discards, then re-running inference."))


def cost_spec(window, channel):
    """Cost runaway: llm.chat calls per agent.invoke request above the SLO."""
    return _ratio_spec(
        COST_ALERT, window, channel,
        numerator=f"service.name = '{TARGET_SERVICE}' AND name = 'llm.chat'",
        denominator=f"service.name = '{TARGET_SERVICE}' AND name = 'agent.invoke'",
        target=COST_SLO_MAX_CALLS_PER_REQ, severity="warning", slo_label="cost_runaway",
        summary=f"LLM calls per request above the {COST_SLO_MAX_CALLS_PER_REQ}/request SLO",
        description=(f"{TARGET_SERVICE} is averaging more than "
                     f"{COST_SLO_MAX_CALLS_PER_REQ} llm.chat calls per agent.invoke "
                     "request: a runaway, token-burning loop (bill shock)."))


def heal_spec(window, channel):
    """Backstop: an agent.heal cycle ended unhealed. NOTIFY-ONLY (never a trigger)."""
    return {
        "alert": HEAL_ALERT,
        "alertType": "TRACES_BASED_ALERT",
        "ruleType": "threshold_rule",
        "version": "v5",
        "evalWindow": window,
        "frequency": "1m",
        "condition": {
            "compositeQuery": {
                "queryType": "builder",
                "panelType": "graph",
                "queries": [_bq("A", (f"service.name = '{HEALER_SERVICE}' "
                                      "AND name = 'agent.heal' AND heal.healed = false"))],
            },
            "target": 0,
            "op": "above",
            "matchType": "at_least_once",
            "selectedQueryName": "A",
        },
        "preferredChannels": [channel] if channel else [],
        # heal_role=notify tells heal_bridge.py this is a page, NOT a trigger, so a
        # failed heal can never launch another heal (no feedback loop).
        "labels": {"severity": "critical", "slo": "heal_backstop", "heal_role": "notify"},
        "annotations": {
            "summary": "The self-healer could not auto-resolve an incident",
            "description": ("An agent.heal cycle ended with heal.healed=false: the "
                            "remediation did not hold on verify and was rolled back, "
                            "or the incident was escalated. A human should take over."),
        },
    }


ALERT_SPECS = [retry_spec, cost_spec, heal_spec]


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
        print(f"  {(r.get('state') or '?'):10s}  {r.get('alert') or r.get('alertName')}  "
              f"(id={r.get('id')}, win={r.get('evalWindow')}, chans={r.get('preferredChannels')})")


def main():
    ap = argparse.ArgumentParser(description="Ensure the self-healing loop's SigNoz alerts")
    ap.add_argument("--ensure", action="store_true", help="create or update all three alerts")
    ap.add_argument("--status", action="store_true", help="print every rule's live state")
    ap.add_argument("--window", default=os.getenv("HEAL_ALERT_WINDOW", "5m"),
                    help="eval window (short = resolves quickly after a heal; default 5m)")
    ap.add_argument("--channel", default=os.getenv("HEAL_ALERT_CHANNEL", "local-webhook"),
                    help="notification channel for the webhook push path")
    args = ap.parse_args()
    ok = True
    if args.ensure:
        ok = ensure(args.window, args.channel)
    if args.status or not args.ensure:
        status()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
