"""Create a 'Retry Tax' dashboard in SigNoz via the API (v5 builder schema).

Quantifies the cost of dropped/retried LLM responses: retried calls, wasted
tokens, retry cost, and a control-vs-chaos comparison grouped by experiment.id.
Auth uses the durable service-account API key in blog/.signoz_api_key.
"""
import json
import sys
import uuid
import urllib.request
from pathlib import Path

BASE = "http://localhost:8080"
KEY = Path("blog/../.signoz_api_key").read_text().strip()

SVC = "observable-agent"
LLM = f"service.name = '{SVC}' AND name = 'llm.chat'"
COHORTS = f"{LLM} AND (experiment.id = 'rt-control' OR experiment.id = 'rt-chaos')"
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


def widget(title, panel, query_data, ptype="graph", yunit="none", desc="",
           decimals=2):
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


widgets = []
layout = []


def add(w_id, w, x, y, wdt, h):
    widgets.append(w)
    layout.append({"h": h, "i": w_id, "moved": False, "static": False,
                   "w": wdt, "x": x, "y": y})


# Row 0: four value tiles — the tax, in absolute terms
id1, w1 = widget("LLM Calls (all)", "value", [qd("count()", "calls", filt=COHORTS)], "value")
id2, w2 = widget("Retried LLM Calls", "value", [qd("count()", "retried", filt=DROPPED)], "value")
id3, w3 = widget("Wasted Tokens", "value",
                 [qd("sum(gen_ai.usage.total_tokens)", "wasted", filt=DROPPED)], "value")
id4, w4 = widget("Retry Cost (USD)", "value",
                 [qd("sum(gen_ai.usage.cost_usd)", "retry $", filt=DROPPED)], "value",
                 decimals=6)
add(id1, w1, 0, 0, 3, 4)
add(id2, w2, 3, 0, 3, 4)
add(id3, w3, 6, 0, 3, 4)
add(id4, w4, 9, 0, 3, 4)

# Row 1: control vs chaos, side by side
id5, w5 = widget(
    "Total Tokens: control vs chaos", "bar",
    [qd("sum(gen_ai.usage.total_tokens)", "{{experiment.id}}",
        filt=COHORTS, gb=groupby("experiment.id"))],
    "bar", yunit="short")
id6, w6 = widget(
    "LLM Calls: control vs chaos", "bar",
    [qd("count()", "{{experiment.id}}", filt=COHORTS, gb=groupby("experiment.id"))],
    "bar", yunit="short")
add(id5, w5, 0, 4, 6, 7)
add(id6, w6, 6, 4, 6, 7)

# Row 2: retries over time + cost split
id7, w7 = widget(
    "Retries Over Time", "graph",
    [qd("count()", "dropped attempts", filt=DROPPED)],
    "graph", yunit="short")
id8, w8 = widget(
    "Cost by Cohort (USD)", "pie",
    [qd("sum(gen_ai.usage.cost_usd)", "{{experiment.id}}",
        filt=COHORTS, gb=groupby("experiment.id"))],
    "pie", decimals=6)
id9, w9 = widget(
    "Wasted Tokens by Model", "pie",
    [qd("sum(gen_ai.usage.total_tokens)", "{{gen_ai.request.model}}",
        filt=DROPPED, gb=groupby("gen_ai.request.model"))],
    "pie")
add(id7, w7, 0, 11, 6, 7)
add(id8, w8, 6, 11, 3, 7)
add(id9, w9, 9, 11, 3, 7)

dashboard = {
    "title": "Retry Tax: the hidden cost of agent retries",
    "description": "What dropped/retried LLM responses cost an agent: retried "
                   "calls, wasted tokens, retry cost, and a control-vs-chaos "
                   "comparison. Data from observable-agent (llm.chat spans).",
    "tags": ["llm", "genai", "agent", "retry", "hackathon"],
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
    Path("blog/retrytax_dashboard_uuid.txt").write_text(str(du))
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode()[:1500])
    sys.exit(1)
