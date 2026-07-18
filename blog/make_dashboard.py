"""Create an 'LLM & Agent Observability' dashboard in SigNoz via the API.

Uses the v5 expression query-builder schema (learned from SigNoz's official
anthropic dashboard) against our own gen_ai.* span attributes.
"""
import json
import sys
import uuid
import urllib.request

BASE = "http://localhost:8080"
with open("blog/token.txt") as fh:
    TOKEN = fh.read().strip()

SVC = "observable-agent"
LLM = f"service.name = '{SVC}' AND name = 'llm.chat'"
ALL = f"service.name = '{SVC}'"


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


# Row 0: four value tiles
id1, w1 = widget("LLM Calls", "value", [qd("count()", "calls")], "value")
id2, w2 = widget("Input Tokens", "value", [qd("sum(gen_ai.usage.input_tokens)", "in")], "value")
id3, w3 = widget("Output Tokens", "value", [qd("sum(gen_ai.usage.output_tokens)", "out")], "value")
id4, w4 = widget("Total Cost (USD)", "value", [qd("sum(gen_ai.usage.cost_usd)", "cost")], "value",
                 decimals=5)
add(id1, w1, 0, 0, 3, 4)
add(id2, w2, 3, 0, 3, 4)
add(id3, w3, 6, 0, 3, 4)
add(id4, w4, 9, 0, 3, 4)

# Row 1: token usage over time + LLM latency by model
id5, w5 = widget(
    "Token Usage Over Time", "graph",
    [qd("sum(gen_ai.usage.input_tokens)", "input tokens", name="A"),
     qd("sum(gen_ai.usage.output_tokens)", "output tokens", name="B")],
    "graph", yunit="short")
id6, w6 = widget(
    "LLM Latency p95 / p99 by Model", "graph",
    [qd("p95(duration_nano)", "p95 {{gen_ai.request.model}}", gb=groupby("gen_ai.request.model"), name="A"),
     qd("p99(duration_nano)", "p99 {{gen_ai.request.model}}", gb=groupby("gen_ai.request.model"), name="B")],
    "graph", yunit="ns")
add(id5, w5, 0, 4, 6, 7)
add(id6, w6, 6, 4, 6, 7)

# Row 2: span latency by operation + two pies
id7, w7 = widget(
    "Span Latency p95 by Operation", "graph",
    [qd("p95(duration_nano)", "{{name}}", filt=ALL, gb=groupby("name"))],
    "graph", yunit="ns")
id8, w8 = widget(
    "Cost by Model", "pie",
    [qd("sum(gen_ai.usage.cost_usd)", "{{gen_ai.request.model}}", gb=groupby("gen_ai.request.model"))],
    "pie", decimals=5)
id9, w9 = widget(
    "LLM Calls by Model", "pie",
    [qd("count()", "{{gen_ai.request.model}}", gb=groupby("gen_ai.request.model"))],
    "pie")
add(id7, w7, 0, 11, 6, 7)
add(id8, w8, 6, 11, 3, 7)
add(id9, w9, 9, 11, 3, 7)

dashboard = {
    "title": "LLM & Agent Observability",
    "description": "Traces-derived LLM/agent telemetry from observable-agent: "
                   "tokens, cost, LLM latency by model, and per-operation latency.",
    "tags": ["llm", "genai", "agent", "hackathon"],
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
    headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())
    data = resp.get("data", resp)
    du = data.get("uuid") or data.get("id") or data
    print("CREATED dashboard uuid:", du)
    with open("blog/dashboard_uuid.txt", "w") as fh:
        fh.write(str(du))
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode()[:1500])
    sys.exit(1)
