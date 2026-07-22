"""Create the 'GreenOps carbon heal' dashboard in SigNoz (v5 builder schema).

This is the SELF-HEALER's carbon view: the cross-track finale where the Track 03
WattTrace energy verdict becomes a Track 01 heal SLO. It tells the money-and-grams
story from telemetry alone, pre heal versus post heal, on the ``self-healer`` service:

  * SCOREBOARD -- the healer's carbon record (counter metrics + agent.heal spans):
                  heals run, carbon SLO breaches detected in SigNoz, heals verified
                  back in bounds, and the wall time per heal.
  * TRACES  -- where the joules actually go. The token usage the energy model prices
               (``gen_ai.usage.output_tokens`` on the managed workload's llm.chat
               spans, per rollout cohort), and the slice of it burned on
               dropped-and-retried calls: the retry tax in energy, made queryable.
               Each heal cycle rolls out a pre cohort under the fault and a post
               cohort under the fix, so the priced token usage grouped by cohort
               shows the pre cohort burning more than the post cohort: that gap is
               the carbon and joules the heal saved per answer.

This board is DISTINCT from the WattTrace GreenOps board (service ``watttrace``,
the standalone energy audit). This one is the healer closing a carbon breach.

Every panel query is re-run through /api/v5/query_range before the dashboard is
created, so the script proves each panel resolves (does not error) on the schema it
ships with. Set the dashboard time range to the last 30 min to isolate one heal run.
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
LLM = f"{WORKLOAD} AND name = 'llm.chat'"
DROPPED = f"{LLM} AND retry.reason = 'response_dropped'"


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


def qd_trace(expr, legend="", filt=LLM, gb=None, name="A"):
    q = _base_qd(name, gb)
    q.update({"aggregations": [{"expression": expr}], "dataSource": "traces",
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
# Every panel is a trace aggregation or a counter metric, the schema that reliably
# resolves here. The footprint-per-answer DROP is told by the energy-basis token
# panels: each heal cycle rolls out a pre cohort under the fault and a post cohort
# under the fix, so grouping the priced token usage by cohort shows pre high, post low.
PANELS = [
    # Row 0 -- the healer's carbon scoreboard -----------------------------------
    ("Carbon heals run",
     [qd_trace("count()", "cycles", filt=f"{HEALER} AND name = 'agent.heal'")],
     "value", "short", "TRACES: agent.heal loops the self-healer ran. Each one detects the "
                       "GreenOps carbon SLO breach in SigNoz, remediates behind the policy gate, "
                       "and re-verifies in SigNoz.", 0),
    ("Carbon SLO breaches detected",
     [qd_metric("heal.slo.breach", "breaches", filt="slo = 'carbon_slo'")],
     "value", "short", "METRICS: times the healer detected the GreenOps carbon SLO breached "
                       "in SigNoz (share of inference energy wasted on retries over the floor).", 0),
    ("Carbon heals verified",
     [qd_metric("heal.result", "healed", filt="slo = 'carbon_slo' AND healed = 'true'")],
     "value", "short", "METRICS: carbon incidents the healer closed and then re-verified in "
                       "SigNoz as back in bounds. Fail closed: an unjudgeable run never counts.", 0),
    ("Heal wall time (agent.heal duration)",
     [qd_trace("avg(duration_nano)", "mttr", filt=f"{HEALER} AND name = 'agent.heal'")],
     "value", "ns", "TRACES: how long a heal took end to end, from breach detected to fix "
                    "verified, measured as the agent.heal span duration. Zero humans paged.", 0),
    # Row 1 -- where the joules go: the energy basis, per cohort, from traces ----
    ("Inference tokens by cohort (the energy basis): pre vs post",
     [qd_trace("sum(gen_ai.usage.output_tokens)", "{{experiment.id}}", filt=LLM,
               gb=groupby("experiment.id"))],
     "bar", "short", "TRACES: the token usage the energy model prices to joules, on the managed "
                     "workload's llm.chat spans, per rollout cohort. The pre cohort (under the "
                     "fault) burns more than the post cohort (under the fix): that gap is the "
                     "energy and carbon the heal saved per answer.", 0),
    ("Wasted energy: tokens on dropped retries by cohort",
     [qd_trace("sum(gen_ai.usage.output_tokens)", "{{experiment.id}}", filt=DROPPED,
               gb=groupby("experiment.id"))],
     "bar", "short", "TRACES: the token slice burned on dropped-and-retried llm.chat calls, per "
                     "cohort: the retry tax in energy. The pre cohort carries it; the post cohort, "
                     "after the heal, should be at or near zero.", 0),
    # Row 2 -- the heal loop itself + the tax clearing over time -----------------
    ("agent.heal carbon loop breakdown (spans)",
     [qd_trace("count()", "{{name}}", filt=HEALER, gb=groupby("name"))],
     "pie", "none", "TRACES: the healer's own span tree on the self-healer service: canary "
                    "rollouts, detect, decide (the local model reads the incident via MCP), and "
                    "verify. This is the governed loop that clears the carbon breach.", 0),
    ("Dropped-and-retried calls over time (workload)",
     [qd_trace("count()", "dropped attempts", filt=DROPPED)],
     "graph", "short", "TRACES: the wasted retry calls that burn energy for no verified answer, "
                       "over time. They appear while the workload is sick and fall to zero once "
                       "the healer applies and verifies the fix.", 0),
]

widgets, layout = [], []


def add(w_id, w, x, y, wdt, h):
    widgets.append(w)
    layout.append({"h": h, "i": w_id, "moved": False, "static": False,
                   "w": wdt, "x": x, "y": y})


# fixed grid (12 cols): row0 four value tiles, row1 two bars, row2 pie + graph
POS = [(0, 0, 3, 4), (3, 0, 3, 4), (6, 0, 3, 4), (9, 0, 3, 4),
       (0, 4, 6, 7), (6, 4, 6, 7),
       (0, 11, 6, 7), (6, 11, 6, 7)]


def qd_to_spec(qd):
    """Translate a dashboard queryData into a query_range builder spec, so we can
    independently prove the panel resolves before shipping it."""
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
            cols = res.get("columns") or []
            agg_idx = [i for i, c in enumerate(cols)
                       if c.get("columnType") == "aggregation"]
            for row in (res.get("data") or []):
                if any(row[i] is not None for i in agg_idx) if agg_idx else bool(row):
                    n += 1
        return True, n
    except urllib.error.HTTPError as e:
        return False, e.read().decode()[:160]


print("Verifying every panel query resolves via /api/v5/query_range ...")
all_ok = True
for i, (title, qds, ptype, yunit, desc, dec) in enumerate(PANELS):
    ok, info = verify(qds[0])
    signal = qds[0]["dataSource"].upper()
    flag = "ok" if ok else "FAIL"
    print(f"  [{flag}] {signal:7} {title:52} series={info}")
    all_ok = all_ok and ok
    wid, w = widget(title, qds, ptype, yunit, desc, dec)
    x, y, wdt, h = POS[i]
    add(wid, w, x, y, wdt, h)

if not all_ok:
    print("\nAborting: at least one panel query errored (schema issue).")
    sys.exit(1)

dashboard = {
    "title": "GreenOps carbon heal: footprint per answer, pre vs post (self-healer)",
    "description": "The cross-track finale, from telemetry alone: the Track 03 WattTrace energy "
                   "verdict wired in as a Track 01 heal SLO, on the self-healer service. The "
                   "scoreboard tiles carry the healer's carbon record: heals run, carbon SLO "
                   "breaches detected in SigNoz, heals verified back in bounds, and the wall time "
                   "per heal. The traces show where the joules go: each heal cycle rolls out a pre "
                   "cohort under the fault and a post cohort under the fix, so the token usage the "
                   "energy model prices, grouped by cohort, shows the pre cohort burning more than "
                   "the post cohort. That gap, and the slice of it wasted on dropped-and-retried "
                   "calls (the retry tax in energy), is the carbon and joules the heal saved per "
                   "answer. Every energy figure is a modelled estimate, never a hardware reading. "
                   "Set the time range to the last 30 min to isolate one heal run.",
    "tags": ["self-healing", "greenops", "carbon", "energy", "sustainability", "finale",
             "traces", "metrics", "track01", "track03", "hackathon"],
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
    Path(__file__).with_name("heal_carbon_dashboard_uuid.txt").write_text(str(du))
    export = data.get("data", dashboard)
    Path(__file__).with_name("dashboards").mkdir(exist_ok=True)
    out = Path(__file__).with_name("dashboards") / "greenops-carbon-heal.json"
    out.write_text(json.dumps(export, indent=2))
    print("Exported importable JSON ->", out)
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode()[:1500])
    sys.exit(1)
