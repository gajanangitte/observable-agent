"""Ensure the SigNoz alert that triggers the healer exists (and is demo-tuned).

The self-healing loop is driven by a REAL SigNoz threshold alert on the managed
workload: when ``observable-agent`` emits ``llm.chat`` spans carrying
``retry.reason = response_dropped`` (the retry tax), the alert fires;
``heal_bridge.py`` turns that firing into a governed heal; once the workload is
fixed the same alert resolves. This script creates or updates that alert
idempotently so the demo is reproducible.

  python heal_alert.py --ensure                 # create/update the retry-tax alert
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

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.getenv("SIGNOZ_UI", "http://localhost:8080").rstrip("/")
KEY = (Path(HERE) / ".signoz_api_key").read_text().strip()

ALERT_NAME = "Agent Retry Tax: LLM responses being retried"


def spec(window, channel):
    """The v5 threshold alert: fire when a dropped-and-retried llm.chat span
    appears on the managed workload within the eval window."""
    return {
        "alert": ALERT_NAME,
        "alertType": "TRACES_BASED_ALERT",
        "ruleType": "threshold_rule",
        "version": "v5",
        "evalWindow": window,
        "frequency": "1m",
        "condition": {
            "compositeQuery": {
                "queryType": "builder",
                "panelType": "graph",
                "queries": [{
                    "type": "builder_query",
                    "spec": {
                        "name": "A",
                        "signal": "traces",
                        "aggregations": [{"expression": "count()"}],
                        "stepInterval": "1m",
                        "filter": {"expression": (
                            "service.name = 'observable-agent' AND name = 'llm.chat' "
                            "AND retry.reason = 'response_dropped'")},
                    },
                }],
            },
            "target": 0,
            "op": "above",
            "matchType": "at_least_once",
            "selectedQueryName": "A",
        },
        "preferredChannels": [channel] if channel else [],
        "labels": {"severity": "warning"},
        "annotations": {
            "summary": "The agent is retrying LLM calls (retry tax)",
            "description": ("llm.chat spans with retry.reason=response_dropped are "
                            "appearing: the agent burned tokens on a response it "
                            "discarded, then re-ran inference."),
        },
    }


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
    body = spec(window, channel)
    existing = find(ALERT_NAME)
    if existing:
        rid = existing["id"]
        st, resp = _req("PUT", f"/api/v1/rules/{rid}", body)
        print(f"UPDATE rule {rid} -> HTTP {st}")
    else:
        st, resp = _req("POST", "/api/v1/rules", body)
        print(f"CREATE rule -> HTTP {st}")
    print(resp[:500])
    return st < 300


def status():
    for r in rules():
        print(f"  {(r.get('state') or '?'):10s}  {r.get('alert') or r.get('alertName')}  "
              f"(id={r.get('id')}, win={r.get('evalWindow')}, chans={r.get('preferredChannels')})")


def main():
    ap = argparse.ArgumentParser(description="Ensure the healer's SigNoz trigger alert")
    ap.add_argument("--ensure", action="store_true", help="create or update the alert")
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
