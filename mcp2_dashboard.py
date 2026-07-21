"""Create the 'MCP Contract Lab: Reliability Certification' dashboard in SigNoz (Track 02).

This is the Query Builder view of the MCP² Contract Lab. It reads the certification
of an MCP server from all three OpenTelemetry signals at once, the same shape the
self-healing dashboard uses, but pointed at the protocol layer instead of the agent:

  * METRICS  -- the certification verdicts (mcp.cert.contract by status, the grade
               per run, the UNKNOWN blind-spot count) and the auto-instrumentation
               call counters (mcp.client.calls by tool and status).
  * TRACES   -- the instrumented mcp.tools/call spans (real read-call p95 latency)
               and the mcp.cert.suite run roots.
  * LOGS     -- the structured certification lifecycle line (mcp2.event by type).

Every panel query is re-run through /api/v5/query_range before the dashboard is
created, so the script proves each panel resolves (not an error) on the schema it
ships with. Set the dashboard time range to the last 3 hours to cover a few runs.
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

SVC = "service.name = 'mcp-contract-lab'"
SUITE = f"{SVC} AND name = 'mcp.cert.suite'"
CALLS = f"{SVC} AND name = 'mcp.tools/call'"


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


def qd_trace(expr, legend="", filt=CALLS, gb=None, name="A"):
    q = _base_qd(name, gb)
    q.update({"aggregations": [{"expression": expr}], "dataSource": "traces",
              "filter": {"expression": filt}, "legend": legend})
    return q


def qd_log(legend="", filt=SVC, gb=None, name="A"):
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
    # Row 0 -- headline verdicts ------------------------------------------------
    ("Certification runs", [qd_trace("count()", "runs", filt=SUITE)],
     "value", "short", "TRACES: each mcp.cert.suite root span is one full certification run.", 0),
    ("Coverage blind spots (UNKNOWN)", [qd_metric("mcp.cert.contract", "blind",
                                                  filt="status = 'UNKNOWN'")],
     "value", "short", "METRICS: contracts the lab could NOT evaluate this window. Fail-closed "
                       "honesty: a check it cannot run is UNKNOWN, never a silent pass.", 0),
    ("Contract verdicts (PASS / BREACH / UNKNOWN)", [qd_metric("mcp.cert.contract", "{{status}}",
                                                              gb=groupby("status"))],
     "pie", "none", "METRICS: mcp.cert.contract split by status -- the full verdict mix across runs.", 0),
    # Row 1 -- call health + which contract broke -------------------------------
    ("Read-call latency p95", [qd_trace("p95(duration_nano)", "p95", filt=CALLS)],
     "value", "ns", "TRACES: p95 of the instrumented mcp.tools/call spans. This is what the "
                    "latency_slo contract certifies against.", 0),
    ("Instrumented MCP calls by tool", [qd_metric("mcp.client.calls", "{{mcp.tool.name}}",
                                                 gb=groupby("mcp.tool.name"))],
     "bar", "short", "METRICS: mcp.client.calls by tool -- every tools/call the lab drove, "
                     "auto-captured with zero server changes.", 0),
    ("Breaches by contract", [qd_metric("mcp.cert.contract", "{{contract}}",
                                       filt="status = 'BREACH'", gb=groupby("contract"))],
     "pie", "none", "METRICS: which reliability contract actually broke (status = BREACH). "
                    "Each fault flips exactly one cell, so this points straight at the defect.", 0),
    # Row 2 -- run outcomes + lifecycle -----------------------------------------
    ("Calls ok vs error", [qd_metric("mcp.client.calls", "{{status}}",
                                     gb=groupby("status"))],
     "value", "short", "METRICS: mcp.client.calls by status -- ok vs error across the probe traffic.", 0),
    ("Certification grade per run", [qd_metric("mcp.cert.grade", "{{grade}}",
                                              gb=groupby("grade"))],
     "bar", "short", "METRICS: mcp.cert.grade by grade -- CERTIFIED / PARTIAL / FAILED / BLIND "
                     "per run. This is the CI gate turned into a time series.", 0),
    ("Certification lifecycle events", [qd_log("{{mcp2.event}}",
                                              filt=f"{SVC} AND mcp2.event EXISTS",
                                              gb=groupby("mcp2.event"))],
     "pie", "none", "LOGS: the structured certification log, one row per lifecycle step "
                    "(suite.start, contract.verdict, suite.done), correlated to the run trace.", 0),
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
        for res in (d.get("data", {}).get("data", {}).get("results") or []):
            for a in (res.get("aggregations") or []):
                n += len(a.get("series") or [])
        return True, n
    except urllib.error.HTTPError as e:
        return False, e.read().decode()[:160]


print("Verifying every panel query resolves via /api/v5/query_range ...")
all_ok = True
for i, (title, qds, ptype, yunit, desc, dec) in enumerate(PANELS):
    ok, info = verify(qds[0])
    signal = qds[0]["dataSource"].upper()
    flag = "ok" if ok else "FAIL"
    print(f"  [{flag}] {signal:7} {title:42} series={info}")
    all_ok = all_ok and ok
    wid, w = widget(title, qds, ptype, yunit, desc, dec)
    x, y, wdt, h = POS[i]
    add(wid, w, x, y, wdt, h)

if not all_ok:
    print("\nAborting: at least one panel query errored (schema issue).")
    sys.exit(1)

dashboard = {
    "title": "MCP Contract Lab: Reliability Certification (traces + metrics + logs)",
    "description": "One Query Builder dashboard certifies an MCP server from all three "
                   "OpenTelemetry signals: metrics (mcp.cert.contract verdicts, the grade per "
                   "run, the UNKNOWN blind-spot count, and the mcp.client.calls auto-instrumentation "
                   "counters), traces (the instrumented mcp.tools/call spans and their p95, plus the "
                   "mcp.cert.suite run roots), and logs (the structured certification lifecycle line). "
                   "Set the time range to the last 3 hours.",
    "tags": ["mcp", "contract-lab", "reliability", "traces", "metrics", "logs",
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
    Path(__file__).with_name("mcp2_dashboard_uuid.txt").write_text(str(du))
    export = data.get("data", dashboard)
    Path(__file__).with_name("dashboards").mkdir(exist_ok=True)
    out = Path(__file__).with_name("dashboards") / "mcp2-contract-lab.json"
    out.write_text(json.dumps(export, indent=2))
    print("Exported importable JSON ->", out)
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode()[:1500])
    sys.exit(1)
