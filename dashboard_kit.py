"""ProofKit: the shared Query Builder dashboard core for this repo.

Every artifact in this project ships a SigNoz dashboard that proves its signals are
real by self verifying each panel against ``/api/v5/query_range`` before it is
created. That machinery (the builder query shapes, the widget envelope, the
translation to a query_range spec, and the dual shape result counter that handles
BOTH the metric ``aggregations[].series[]`` and the trace ``columns[] + data``
scalar shapes) was copied across four builders. This module is the single home for
it, so a new artifact gets a self verifying dashboard for free and a fix lands once.

It is import safe offline: the API key is read lazily (only when a live call is
made), so the pure helpers (``qd_trace``, ``qd_to_spec``, ``count_results``) unit
test with no key file and no network.
"""
from __future__ import annotations

import json
import os
import uuid
import urllib.request
import urllib.error
from pathlib import Path

BASE = os.getenv("SIGNOZ_BASE", "http://localhost:8080")
_KEY_FILE = Path(__file__).with_name(".signoz_api_key")


def api_key() -> str:
    """Read the SigNoz API key lazily so importing this module never needs it."""
    return _KEY_FILE.read_text().strip()


def headers() -> dict:
    return {"SIGNOZ-API-KEY": api_key(), "Content-Type": "application/json"}


# --- builder query shapes ----------------------------------------------------
def groupby(key: str):
    return [{
        "dataType": "string",
        "id": f"{key}--string--tag",
        "isColumn": False,
        "isJSON": False,
        "key": key,
        "type": "tag",
    }]


def base_qd(name: str = "A", gb=None) -> dict:
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


def qd_trace(expr: str, legend: str = "", filt: str = "", gb=None, name: str = "A") -> dict:
    """A traces builder query. ``filt`` is required per call (each builder scopes to
    its own service and span name), so this stays generic across artifacts."""
    q = base_qd(name, gb)
    q.update({"aggregations": [{"expression": expr}], "dataSource": "traces",
              "filter": {"expression": filt}, "legend": legend})
    return q


def qd_metric(metric: str, legend: str = "", gb=None, filt: str = "",
              time_agg: str = "count", space_agg: str = "sum", name: str = "A") -> dict:
    q = base_qd(name, gb)
    q.update({
        "aggregations": [{"metricName": metric, "temporality": "",
                          "timeAggregation": time_agg, "spaceAggregation": space_agg}],
        "dataSource": "metrics",
        "filter": {"expression": filt},
        "legend": legend,
    })
    return q


def widget(title: str, query_data, ptype: str = "graph", yunit: str = "none",
           desc: str = "", decimals: int = 2):
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


def qd_to_spec(qd: dict) -> dict:
    """Translate a dashboard queryData into a query_range builder spec, so a panel
    can be proven to resolve to data before it is shipped."""
    gb = [{"name": g["key"]} for g in qd.get("groupBy", [])]
    return {"name": qd["queryName"], "signal": qd["dataSource"],
            "aggregations": qd["aggregations"],
            "filter": qd.get("filter", {"expression": ""}), "groupBy": gb}


def count_results(response: dict) -> int:
    """Count the data points in a query_range response, handling BOTH shapes:
    the metric scalar shape (aggregations[].series[]) and the trace/log scalar
    shape (columns[] with columnType 'aggregation' plus data rows). Pure, so the
    self verification logic is unit tested without SigNoz."""
    n = 0
    for res in (response.get("data", {}).get("data", {}).get("results") or []):
        for a in (res.get("aggregations") or []):
            n += len(a.get("series") or [])
        cols = res.get("columns") or []
        agg_idx = [i for i, c in enumerate(cols) if c.get("columnType") == "aggregation"]
        for row in (res.get("data") or []):
            if (any(row[i] is not None for i in agg_idx) if agg_idx else bool(row)):
                n += 1
    return n


def verify(qd: dict, window_hours: int = 6):
    """Prove one panel query resolves (not an error) against query_range. Returns
    (ok, count) on success or (False, error_text) on an HTTP error."""
    import time
    now = int(time.time() * 1000)
    body = {"start": now - window_hours * 3600 * 1000, "end": now, "requestType": "scalar",
            "compositeQuery": {"queries": [{"type": "builder_query", "spec": qd_to_spec(qd)}]}}
    req = urllib.request.Request(f"{BASE}/api/v5/query_range",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers=headers())
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read())
        return True, count_results(d)
    except urllib.error.HTTPError as e:
        return False, e.read().decode()[:160]


def ship(title: str, description: str, tags, panels, positions,
         uuid_filename: str, export_filename: str, window_hours: int = 6):
    """Self verify every panel, then create the dashboard and export importable JSON.

    ``panels`` is a list of (title, queryData, panelType, yUnit, description,
    decimals); ``positions`` is the matching list of (x, y, w, h) grid slots.
    Returns (uuid, all_ok); on a failed verification it aborts before creating."""
    widgets, layout = [], []
    all_ok = True
    print("Verifying every panel query resolves via /api/v5/query_range ...")
    for i, (ptitle, qds, ptype, yunit, desc, dec) in enumerate(panels):
        ok, info = verify(qds[0], window_hours)
        signal = qds[0]["dataSource"].upper()
        print(f"  [{'ok' if ok else 'FAIL'}] {signal:7} {ptitle:54} n={info}")
        all_ok = all_ok and ok
        wid, w = widget(ptitle, qds, ptype, yunit, desc, dec)
        x, y, wdt, h = positions[i]
        widgets.append(w)
        layout.append({"h": h, "i": wid, "moved": False, "static": False,
                       "w": wdt, "x": x, "y": y})
    if not all_ok:
        print("\nAborting: at least one panel query errored (schema issue).")
        return None, False

    dashboard = {
        "title": title,
        "description": description,
        "tags": list(tags),
        "layout": layout,
        "panelMap": {},
        "variables": {},
        "version": "v5",
        "uploadedGrafana": False,
        "widgets": widgets,
    }
    body = json.dumps(dashboard).encode()
    req = urllib.request.Request(f"{BASE}/api/v1/dashboards", data=body, method="POST",
                                 headers=headers())
    try:
        with urllib.request.urlopen(req) as r:
            resp = json.loads(r.read())
        data = resp.get("data", resp)
        du = data.get("uuid") or data.get("id") or data
        print("\nCREATED dashboard uuid:", du)
        Path(__file__).with_name(uuid_filename).write_text(str(du))
        export = data.get("data", dashboard)
        Path(__file__).with_name("dashboards").mkdir(exist_ok=True)
        out = Path(__file__).with_name("dashboards") / export_filename
        out.write_text(json.dumps(export, indent=2))
        print("Exported importable JSON ->", out)
        return du, True
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode()[:1500])
        return None, False
