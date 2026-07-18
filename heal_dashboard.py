"""Create a 'Self-Healing SRE Sidekick' dashboard in SigNoz (v5 builder schema).

Tells the closed-loop story from telemetry alone:
  * the healer's own activity  (service = self-healer: heal cycles, remediations,
    the agent.heal span breakdown), and
  * the managed workload's before/after  (service = observable-agent: LLM calls
    and retried calls per rollout cohort -- pre cohorts carry the retry tax, post
    cohorts don't).

All panels are trace aggregations (the schema that is known to work here); set the
dashboard time range to the last 30 min to isolate a single heal run.
"""
import json
import sys
import uuid
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://localhost:8080"
KEY = Path(__file__).with_name(".signoz_api_key").read_text().strip()

HEALER = "service.name = 'self-healer'"
WORKLOAD = "service.name = 'observable-agent'"
LLM = f"{WORKLOAD} AND name = 'llm.chat'"
DROPPED = f"{LLM} AND retry.reason = 'response_dropped'"
HEALCYCLE = f"{HEALER} AND name = 'agent.heal'"
REMEDIATION = (f"{HEALER} AND (name = 'tool.disable_fault_injection' "
               f"OR name = 'tool.enable_mitigation')")


def groupby(key):
    return [{
        "dataType": "string",
        "id": f"{key}--string--tag",
        "isColumn": False,
        "isJSON": False,
        "key": key,
        "type": "tag",
    }]


def qd(expr, legend="", filt=LLM, gb=None, name="A"):
    return {
        "aggregations": [{"expression": expr}],
        "dataSource": "traces",
        "disabled": False,
        "expression": name,
        "filter": {"expression": filt},
        "functions": [],
        "groupBy": gb or [],
        "having": {"expression": ""},
        "legend": legend,
        "limit": None,
        "orderBy": [],
        "queryName": name,
        "source": "",
        "stepInterval": None,
    }


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


widgets, layout = [], []


def add(w_id, w, x, y, wdt, h):
    widgets.append(w)
    layout.append({"h": h, "i": w_id, "moved": False, "static": False,
                   "w": wdt, "x": x, "y": y})


# Row 0: the healer's scoreboard -------------------------------------------------
id1, w1 = widget("Heal cycles run", [qd("count()", "cycles", filt=HEALCYCLE)], "value")
id2, w2 = widget("Breaches remediated", [qd("count()", "fixes", filt=REMEDIATION)], "value")
id3, w3 = widget("Retried LLM calls (workload)", [qd("count()", "retries", filt=DROPPED)], "value")
id4, w4 = widget("Retry cost chased (USD)",
                 [qd("sum(gen_ai.usage.cost_usd)", "retry $", filt=DROPPED)], "value", decimals=6)
add(id1, w1, 0, 0, 3, 4)
add(id2, w2, 3, 0, 3, 4)
add(id3, w3, 6, 0, 3, 4)
add(id4, w4, 9, 0, 3, 4)

# Row 1: before vs after, per rollout cohort ------------------------------------
id5, w5 = widget("LLM calls by rollout cohort",
                 [qd("count()", "{{experiment.id}}", filt=LLM, gb=groupby("experiment.id"))],
                 "bar", yunit="short",
                 desc="Each canary rollout is one experiment.id. Pre cohorts run under "
                      "the fault; post cohorts run under the healer's fix.")
id6, w6 = widget("Retried calls by rollout cohort (the retry tax)",
                 [qd("count()", "{{experiment.id}}", filt=DROPPED, gb=groupby("experiment.id"))],
                 "bar", yunit="short",
                 desc="Dropped-and-retried llm.chat calls per cohort. Post-heal cohorts "
                      "should show zero.")
add(id5, w5, 0, 4, 6, 7)
add(id6, w6, 6, 4, 6, 7)

# Row 2: the loop itself + retries over time ------------------------------------
id7, w7 = widget("agent.heal loop breakdown (spans)",
                 [qd("count()", "{{name}}", filt=HEALER, gb=groupby("name"))],
                 "pie", desc="The healer's own span tree: detect, decide, canary rollouts, verify.")
id8, w8 = widget("Retries over time (workload)",
                 [qd("count()", "dropped attempts", filt=DROPPED)], "graph", yunit="short")
add(id7, w7, 0, 11, 6, 7)
add(id8, w8, 6, 11, 6, 7)

dashboard = {
    "title": "Self-Healing SRE Sidekick: detect -> diagnose -> act -> verify",
    "description": "A local agent uses SigNoz (via the MCP server) as the sensor in a "
                   "closed control loop: it detects a retry-tax SLO breach, diagnoses it, "
                   "remediates, and verifies the fix -- all traced under agent.heal on the "
                   "self-healer service. Set the time range to the last 30 min to isolate one run.",
    "tags": ["llm", "genai", "agent", "self-healing", "mcp", "hackathon"],
    "layout": layout,
    "panelMap": {},
    "variables": {},
    "version": "v5",
    "uploadedGrafana": False,
    "widgets": widgets,
}

body = json.dumps(dashboard).encode()
req = urllib.request.Request(
    f"{BASE}/api/v1/dashboards", data=body, method="POST",
    headers={"SIGNOZ-API-KEY": KEY, "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())
    data = resp.get("data", resp)
    du = data.get("uuid") or data.get("id") or data
    print("CREATED dashboard uuid:", du)
    Path(__file__).with_name("heal_dashboard_uuid.txt").write_text(str(du))
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode()[:1500])
    sys.exit(1)
