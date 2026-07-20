"""Create the 'Agent Reliability: Three Signals' dashboard in SigNoz (Track 02).

This is a Query Builder showcase: one dashboard that reads the self-healing loop
from all three OpenTelemetry signals at once.

  * TRACES   -- the agent.heal control loop itself (cycles, span breakdown,
               remediations applied).
  * METRICS  -- the healer's own instruments (SLO breaches, heal outcomes,
               how each fix was decided, and the must-stay-zero unsafe counter).
  * LOGS     -- the structured lifecycle log the healer now emits, correlated to
               the heal trace (events by type, and the recorded outcome).

Every panel query is re-run through /api/v5/query_range before the dashboard is
created, so the script proves each panel resolves to data (not an error) on the
schema it ships with. Set the dashboard time range to the last 3 hours to cover a
few heal runs.
"""
import json
import sys
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://localhost:8080"
KEY = Path(__file__).with_name(".signoz_api_key").read_text().strip()
HDRS = {"SIGNOZ-API-KEY": KEY, "Content-Type": "application/json"}

HEALER = "service.name = 'self-healer'"
WORKLOAD = "service.name = 'observable-agent'"
HEALCYCLE = f"{HEALER} AND name = 'agent.heal'"
HEALSPANS = f"{HEALER} AND name != 'agent.heal'"
REMEDIATION = (f"{HEALER} AND (name = 'tool.disable_fault_injection' "
               f"OR name = 'tool.enable_mitigation' OR name = 'tool.set_cost_budget')")


def groupby(key):
    return [{
        "dataType": "string",
        "id": f"{key}--string--tag",
        "isColumn": False,
        "isJSON": False,
        "key": key,
        "type": "tag",
    }]


def _base_qd(name="A", gb=None):
    return {
        "disabled": False,
        "expression": name,
        "functions": [],
        "groupBy": gb or [],
        "having": {"expression": ""},
        "legend": "",
        "limit": None,
        "orderBy": [],
        "queryName": name,
        "source": "",
        "stepInterval": None,
    }


def qd_trace(expr, legend="", filt=HEALCYCLE, gb=None, name="A"):
    q = _base_qd(name, gb)
    q.update({"aggregations": [{"expression": expr}], "dataSource": "traces",
              "filter": {"expression": filt}, "legend": legend})
    return q


def qd_log(legend="", filt=HEALER, gb=None, name="A"):
    q = _base_qd(name, gb)
    q.update({"aggregations": [{"expression": "count()"}], "dataSource": "logs",
              "filter": {"expression": filt}, "legend": legend})
    return q


def qd_metric(metric, legend="", gb=None, filt="", time_agg="count",
              space_agg="sum", name="A"):
    q = _base_qd(name, gb)
    q.update({
        "aggregations": [{"metricName": metric, "temporality": "",
                          "timeAggregation": time_agg, "spaceAggregation": space_agg}],
        "dataSource": "metrics",
        "filter": {"expression": filt},
        "legend": legend,
    })
    return q


def widget(title, query_data, ptype="graph", yunit="none", desc="", decimals=2):
    wid = str(uuid.uuid4())
    return wid, {
        "id": wid,
        "title": title,
        "description": desc,
        "panelTypes": ptype,
        "bucketCount": 30,
        "bucketWidth": 0,
        "columnUnits": {},
        "decimalPrecision": decimals,
        "fillSpans": False,
        "isLogScale": False,
        "isStacked": False,
        "legendPosition": "bottom",
        "mergeAllActiveQueries": False,
        "nullZeroValues": "zero",
        "opacity": "1",
        "softMax": 0,
        "softMin": 0,
        "stackedBarChart": False,
        "thresholds": [],
        "timePreferance": "GLOBAL_TIME",
        "yAxisUnit": yunit,
        "query": {
            "builder": {
                "queryData": query_data,
                "queryFormulas": [],
                "queryTraceOperator": [],
            },
            "clickhouse_sql": [{"disabled": False, "legend": "", "name": "A", "query": ""}],
            "id": str(uuid.uuid4()),
            "promql": [{"disabled": False, "legend": "", "name": "A", "query": ""}],
            "queryType": "builder",
            "unit": "",
        },
    }


# --- panel catalogue: (title, queryData, panelType, yunit, desc, decimals) -----
PANELS = [
    # Row 0 -- TRACES: the control loop -----------------------------------------
    ("Heal cycles run", [qd_trace("count()", "cycles", filt=HEALCYCLE)],
     "value", "short", "TRACES: each agent.heal root span is one detect to verify cycle.", 0),
    ("Remediations applied", [qd_trace("count()", "fixes", filt=REMEDIATION)],
     "value", "short", "TRACES: actuator spans the agent chose and applied.", 0),
    ("agent.heal loop breakdown", [qd_trace("count()", "{{name}}", filt=HEALSPANS,
                                            gb=groupby("name"))],
     "pie", "none", "TRACES: the healer's own span tree -- detect, decide, canary, verify.", 0),
    # Row 1 -- METRICS: the healer's instruments --------------------------------
    ("SLO breaches detected", [qd_metric("heal.slo.breach", "breaches")],
     "value", "short", "METRICS: heal.slo.breach counter -- breaches the healer caught in SigNoz.", 0),
    ("Heal outcomes (healed true/false)", [qd_metric("heal.result", "{{healed}}",
                                                    gb=groupby("healed"))],
     "bar", "short", "METRICS: heal.result counter split by healed -- the verified outcome.", 0),
    ("How each fix was decided", [qd_metric("heal.decision", "{{source}}",
                                           gb=groupby("source"))],
     "pie", "none", "METRICS: heal.decision by source -- memory (recalled) vs llm vs fallback. "
                    "Memory recall is the learning loop in action.", 0),
    ("Unsafe actions (must stay 0)", [qd_metric("heal.unsafe_action", "unsafe")],
     "value", "short", "METRICS: heal.unsafe_action counter -- policy or bound violations. "
                       "The governance gate keeps this at zero.", 0),
    # Row 2 -- LOGS: the structured lifecycle -----------------------------------
    ("Heal lifecycle events by type", [qd_log("{{heal.event}}",
                                             filt=f"{HEALER} AND heal.event EXISTS",
                                             gb=groupby("heal.event"))],
     "bar", "short", "LOGS: the healer's structured log, one row per lifecycle step "
                     "(breach.detected, decision.recall, action.applied, verify, outcome).", 0),
    ("Recorded heal outcomes (logs)", [qd_log("{{heal.healed}}",
                                             filt=f"{HEALER} AND heal.event = 'outcome'",
                                             gb=groupby("heal.healed"))],
     "pie", "none", "LOGS: the outcome log line split by heal.healed -- corroborates the "
                    "metric and the trace from a third signal.", 0),
]

widgets, layout = [], []


def add(w_id, w, x, y, wdt, h):
    widgets.append(w)
    layout.append({"h": h, "i": w_id, "moved": False, "static": False,
                   "w": wdt, "x": x, "y": y})


# fixed 3-per-row grid (12 cols)
POS = [(0, 0, 4, 4), (4, 0, 4, 4), (8, 0, 4, 6),
       (0, 4, 3, 4), (3, 4, 5, 6), (8, 6, 4, 6), (0, 8, 3, 4),
       (0, 12, 6, 7), (6, 12, 6, 7)]


def qd_to_spec(qd):
    """Translate a dashboard queryData into a query_range builder spec, so we can
    independently prove the panel resolves to data before shipping it."""
    signal = qd["dataSource"]
    gb = [{"name": g["key"]} for g in qd.get("groupBy", [])]
    return {"name": qd["queryName"], "signal": signal,
            "aggregations": qd["aggregations"],
            "filter": qd.get("filter", {"expression": ""}), "groupBy": gb}


def verify(qd):
    now = int(time.time() * 1000)
    body = {"start": now - 6 * 3600 * 1000, "end": now, "requestType": "scalar",
            "compositeQuery": {"queries": [{"type": "builder_query", "spec": qd_to_spec(qd)}]}}
    req = urllib.request.Request(f"{BASE}/api/v5/query_range",
                                 data=json.dumps(body).encode(), method="POST", headers=HDRS)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read())
        n = 0
        for res in d["data"]["data"]["results"]:
            for a in res.get("aggregations", []):
                n += len(a.get("series", []))
        return True, n
    except urllib.error.HTTPError as e:
        return False, e.read().decode()[:160]


print("Verifying every panel query resolves via /api/v5/query_range ...")
all_ok = True
for i, (title, qds, ptype, yunit, desc, dec) in enumerate(PANELS):
    ok, info = verify(qds[0])
    signal = qds[0]["dataSource"].upper()
    flag = "ok" if ok else "FAIL"
    print(f"  [{flag}] {signal:7} {title:38} series={info}")
    all_ok = all_ok and ok
    wid, w = widget(title, qds, ptype, yunit, desc, dec)
    x, y, wdt, h = POS[i]
    add(wid, w, x, y, wdt, h)

if not all_ok:
    print("\nAborting: at least one panel query errored (schema issue).")
    sys.exit(1)

dashboard = {
    "title": "Agent Reliability: Three Signals (traces + metrics + logs)",
    "description": "One Query Builder dashboard reads the self-healing loop from all "
                   "three OpenTelemetry signals: traces (the agent.heal control loop), "
                   "metrics (the healer's SLO-breach, outcome, decision-source and "
                   "unsafe-action instruments), and logs (the structured lifecycle line "
                   "correlated to the heal trace). Set the time range to the last 3 hours.",
    "tags": ["llm", "agent", "self-healing", "traces", "metrics", "logs",
             "query-builder", "track02", "hackathon"],
    "layout": layout,
    "panelMap": {},
    "variables": {},
    "version": "v5",
    "uploadedGrafana": False,
    "widgets": widgets,
}

body = json.dumps(dashboard).encode()
req = urllib.request.Request(f"{BASE}/api/v1/dashboards", data=body, method="POST", headers=HDRS)
try:
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())
    data = resp.get("data", resp)
    du = data.get("uuid") or data.get("id") or data
    print("\nCREATED dashboard uuid:", du)
    Path(__file__).with_name("fullsignal_dashboard_uuid.txt").write_text(str(du))
    # export the exact JSON judges can import
    export = data.get("data", dashboard)
    Path(__file__).with_name("dashboards").mkdir(exist_ok=True)
    out = Path(__file__).with_name("dashboards") / "agent-reliability-three-signals.json"
    out.write_text(json.dumps(export, indent=2))
    print("Exported importable JSON ->", out)
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode()[:1500])
    sys.exit(1)
