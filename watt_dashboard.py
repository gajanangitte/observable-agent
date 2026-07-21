"""Create the 'WattTrace GreenOps: energy per verified answer' dashboard (Track 03).

This is the Query Builder view of WattTrace. It reads the energy cost of the local
agent from two OpenTelemetry signals at once, pointed at the thing a token dashboard
cannot show you: the JOULES PER VERIFIED ANSWER, and how much of that energy was
wasted on retries and failed work.

  * TRACES  -- the GreenOps verdict per cohort (watt.cohort spans carrying
               watttrace.status = PASS/BREACH/UNKNOWN and the north-star
               watttrace.joules_per_verified_answer), and the energy-tagged
               llm.chat spans underneath (watttrace.energy.j, its disposition
               useful/wasted, joules per token, and the estimate provenance).
  * METRICS -- watttrace.answer.count, split by whether the answer passed
               verification, so the dashboard shows a verified-answer RATE.

Every energy figure on a span is a MODEL of active-power draw times active time, not
a reading from a hardware sensor, so each estimate is stamped with its provenance
(watttrace.estimate.quality = MEASURED / ESTIMATED / FALLBACK). The dashboard shows
that quality mix directly, so a green board can never quietly hide a guess.

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

SVC = "service.name = 'watttrace'"
COHORT = f"{SVC} AND name = 'watt.cohort'"
LLM = f"{SVC} AND name = 'llm.chat'"


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
PANELS = [
    # Row 0 -- the north star + the verdict -------------------------------------
    ("Energy per verified answer: control vs retry fault (J)",
     [qd_trace("avg(watttrace.joules_per_verified_answer)", "{{watt.cohort}}",
               filt=COHORT, gb=groupby("watt.cohort"))],
     "bar", "none", "TRACES: the north star. Each watt.cohort span carries the joules "
                    "spent per VERIFIED answer. The retry-fault cohort should sit clearly "
                    "above the clean one for the same answers: that gap is wasted energy.", 0),
    ("Budget breaches",
     [qd_trace("count()", "breaches", filt=f"{COHORT} AND watttrace.status = 'BREACH'")],
     "value", "short", "TRACES: cohorts whose energy per verified answer blew the GreenOps "
                       "budget. Fail closed: a run with too few verified answers is UNKNOWN, "
                       "never a silent pass.", 0),
    ("GreenOps verdict mix (PASS / BREACH / UNKNOWN)",
     [qd_trace("count()", "{{watttrace.status}}", filt=COHORT,
               gb=groupby("watttrace.status"))],
     "pie", "none", "TRACES: watt.cohort spans split by verdict. UNKNOWN is an honest "
                    "third state for a run that could not be judged, kept separate from PASS.", 0),
    # Row 1 -- where the energy actually goes -----------------------------------
    ("Joules per token by model",
     [qd_trace("avg(watttrace.energy.j_per_token)", "{{gen_ai.request.model}}",
               filt=LLM, gb=groupby("gen_ai.request.model"))],
     "value", "none", "TRACES: modelled energy per token on the llm.chat spans, by model. "
                      "The efficiency knob: a smaller or better-served model moves this down.", 4),
    ("Wasted retry energy by cohort (J)",
     [qd_trace("sum(watttrace.energy.j)", "{{experiment.id}}",
               filt=f"{LLM} AND watttrace.energy.disposition = 'wasted'",
               gb=groupby("experiment.id"))],
     "bar", "none", "TRACES: energy on llm.chat calls whose work was thrown away (a dropped "
                    "retry or a failed answer). The clean cohort should be near zero; the "
                    "retry-fault cohort carries the tax.", 0),
    ("Energy by disposition (useful vs wasted)",
     [qd_trace("sum(watttrace.energy.j)", "{{watttrace.energy.disposition}}",
               filt=LLM, gb=groupby("watttrace.energy.disposition"))],
     "pie", "none", "TRACES: every joule on llm.chat spans, split into work that produced a "
                    "verified answer and work that was retried or failed. The wasted slice is "
                    "the retry tax made visible.", 0),
    # Row 2 -- latency, provenance honesty, and the verified-answer rate --------
    ("Inference latency p95 by model",
     [qd_trace("p95(duration_nano)", "{{gen_ai.request.model}}", filt=LLM,
               gb=groupby("gen_ai.request.model"))],
     "value", "ns", "TRACES: p95 wall-clock of the llm.chat spans by model. Energy is modelled "
                    "from token counts, so this stays an independent cross-check of the run.", 0),
    ("Estimate provenance (MEASURED / ESTIMATED / FALLBACK)",
     [qd_trace("count()", "{{watttrace.estimate.quality}}", filt=LLM,
               gb=groupby("watttrace.estimate.quality"))],
     "pie", "none", "TRACES: how each energy number was arrived at. A green board that is built "
                    "on FALLBACK guesses is labelled as such, never presented as measured.", 0),
    ("Verified vs unverified answers",
     [qd_metric("watttrace.answer.count", "{{verified}}", gb=groupby("verified"))],
     "pie", "none", "METRICS: watttrace.answer.count split by verification. The denominator of "
                    "the north star: energy only counts against answers that were actually right.", 0),
]

widgets, layout = [], []


def add(w_id, w, x, y, wdt, h):
    widgets.append(w)
    layout.append({"h": h, "i": w_id, "moved": False, "static": False,
                   "w": wdt, "x": x, "y": y})


# fixed grid (12 cols)
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
            # metrics scalar shape: aggregations[].series[]
            for a in (res.get("aggregations") or []):
                n += len(a.get("series") or [])
            # traces scalar shape: columns[] + data rows (one row per group)
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
    "title": "WattTrace GreenOps: energy per verified answer (traces + metrics)",
    "description": "One Query Builder dashboard reads the energy cost of the local agent from "
                   "two OpenTelemetry signals: traces (the watt.cohort GreenOps verdicts carrying "
                   "watttrace.status and the north-star joules per verified answer, plus the "
                   "energy-tagged llm.chat spans with their disposition, joules per token, latency, "
                   "and estimate provenance) and metrics (watttrace.answer.count for the verified "
                   "answer rate). Every energy figure is a modelled estimate stamped with its "
                   "quality, never a hardware reading presented as measured. Set the time range to "
                   "the last 3 hours.",
    "tags": ["watttrace", "greenops", "energy", "carbon", "sustainability", "traces",
             "metrics", "query-builder", "track03", "hackathon"],
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
    Path(__file__).with_name("watt_dashboard_uuid.txt").write_text(str(du))
    export = data.get("data", dashboard)
    Path(__file__).with_name("dashboards").mkdir(exist_ok=True)
    out = Path(__file__).with_name("dashboards") / "watttrace-greenops.json"
    out.write_text(json.dumps(export, indent=2))
    print("Exported importable JSON ->", out)
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode()[:1500])
    sys.exit(1)
