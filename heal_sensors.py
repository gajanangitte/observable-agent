"""The healer's senses: SLO detectors backed by SigNoz.

Every check is a real ``signoz_aggregate_traces`` call through the SigNoz MCP
server, wrapped in an ``mcp.*`` CLIENT span, scoped to one canary cohort
(``experiment.id``) so rollouts never bleed into each other. There is NO LLM in
the detection hot path -- detection and verification are deterministic; the
model is only asked to *decide* what to do about a confirmed breach.
"""
import json
import time

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

TARGET_SERVICE = "observable-agent"   # the managed workload the healer watches
RETRY_SLO_MAX_RATE = 0.05             # > 5% dropped-and-retried llm.chat = breach
LATENCY_SLO_MAX_MS = 60000            # agent.invoke p95 must stay under 60s
COST_SLO_MAX_CALLS_PER_REQ = 6        # > 6 llm.chat per request = runaway-spend breach
NOMINAL_CALL_COST_USD = 0.00004       # fallback per-call cost for the spend headline

tracer = trace.get_tracer("self-healer")
_NS_PER_MS = 1_000_000


def _agg(mcp, args):
    """One signoz_aggregate_traces call, wrapped in an mcp.* span."""
    with tracer.start_as_current_span("mcp.signoz_aggregate_traces", kind=SpanKind.CLIENT) as span:
        span.set_attribute("mcp.server.url", mcp.url)
        span.set_attribute("mcp.transport", "streamable_http")
        span.set_attribute("mcp.method", "tools/call")
        span.set_attribute("mcp.tool.name", "signoz_aggregate_traces")
        span.set_attribute("mcp.request.args", json.dumps(args)[:500])
        raw = mcp.call_tool("signoz_aggregate_traces", args)
        span.set_attribute("mcp.response.bytes", len(raw or ""))
        # SigNoz sometimes appends a plain-text pagination note after the JSON,
        # so decode just the first JSON value rather than json.loads-ing it all.
        try:
            parsed, _ = json.JSONDecoder().raw_decode((raw or "").lstrip())
        except Exception as e:  # noqa: BLE001
            span.set_status(Status(StatusCode.ERROR, f"non-JSON MCP response: {e}"))
            return None
        return parsed


def _scalar(parsed):
    """Pull the single aggregate value out of a signoz_aggregate_traces result."""
    try:
        rows = parsed["data"]["data"]["results"][0]["data"]
        if rows and rows[0] and rows[0][-1] is not None:
            return float(rows[0][-1])
    except Exception:  # noqa: BLE001
        pass
    return None


def _count(mcp, filter_expr, time_range="20m"):
    parsed = _agg(mcp, {
        "aggregation": "count", "aggregateOn": "duration_nano",
        "filter": filter_expr, "timeRange": time_range, "limit": "5",
    })
    val = _scalar(parsed)
    return int(val) if val is not None else 0


def _sum(mcp, attr, filter_expr, time_range="20m"):
    """Best-effort sum of a numeric span attribute (None if not aggregatable)."""
    parsed = _agg(mcp, {
        "aggregation": "sum", "aggregateOn": attr,
        "filter": filter_expr, "timeRange": time_range, "limit": "5",
    })
    return _scalar(parsed)


def retry_slo(mcp, cohort, time_range="20m"):
    """Retry-tax SLO: fraction of a cohort's llm.chat calls that were dropped
    and retried. This is the healer's primary sensor."""
    base = (f"service.name = '{TARGET_SERVICE}' AND experiment.id = '{cohort}' "
            f"AND name = 'llm.chat'")
    total = _count(mcp, base, time_range)
    dropped = _count(mcp, base + " AND retry.reason = 'response_dropped'", time_range)
    rate = (dropped / total) if total else 0.0
    breached = rate > RETRY_SLO_MAX_RATE
    return {
        "slo": "retry_tax", "cohort": cohort,
        "total_llm_calls": total, "dropped": dropped, "retry_rate": round(rate, 3),
        "threshold": RETRY_SLO_MAX_RATE, "breached": breached,
        "headline": (f"{dropped}/{total} llm.chat calls were dropped and retried "
                     f"(retry rate {rate:.0%} vs SLO max {RETRY_SLO_MAX_RATE:.0%}) "
                     f"in cohort '{cohort}'."),
    }


def cost_slo(mcp, cohort, time_range="20m"):
    """Cost/runaway SLO: how many llm.chat calls a cohort burns per request (a
    stuck agent loops and runs up the bill). The healer's bill-shock sensor.

    Breach is measured on calls-per-request (always available via count); the
    dollar spend is summed from the per-call cost attribute when SigNoz can
    aggregate it, else estimated, purely for the headline."""
    svc = f"service.name = '{TARGET_SERVICE}' AND experiment.id = '{cohort}'"
    calls = _count(mcp, svc + " AND name = 'llm.chat'", time_range)
    reqs = _count(mcp, svc + " AND name = 'agent.invoke'", time_range)
    cpr = (calls / reqs) if reqs else float(calls)
    spent = _sum(mcp, "gen_ai.usage.cost_usd", svc + " AND name = 'llm.chat'", time_range)
    if spent is None:
        spent = calls * NOMINAL_CALL_COST_USD
    per_req_spend = (spent / reqs) if reqs else spent
    breached = cpr > COST_SLO_MAX_CALLS_PER_REQ
    return {
        "slo": "cost_runaway", "cohort": cohort,
        "requests": reqs, "llm_calls": calls,
        "calls_per_request": round(cpr, 2),
        "spend_usd": round(spent, 6),
        "spend_per_request_usd": round(per_req_spend, 6),
        "threshold_calls": COST_SLO_MAX_CALLS_PER_REQ, "breached": breached,
        "headline": (f"{calls} llm.chat calls across {reqs} request(s) = "
                     f"{cpr:.1f}/request (~${spent:.6f} spend) vs SLO max "
                     f"{COST_SLO_MAX_CALLS_PER_REQ}/request in cohort '{cohort}'."),
    }


def latency_slo(mcp, cohort, time_range="20m"):
    """Latency SLO: agent.invoke p95 for a cohort. Secondary sensor (model swap)."""
    parsed = _agg(mcp, {
        "aggregation": "p95", "aggregateOn": "duration_nano", "groupBy": "name",
        "filter": (f"service.name = '{TARGET_SERVICE}' AND experiment.id = '{cohort}' "
                   f"AND name = 'agent.invoke'"),
        "timeRange": time_range, "limit": "5",
    })
    val = _scalar(parsed)
    p95_ms = round(val / _NS_PER_MS, 1) if val is not None else None
    breached = p95_ms is not None and p95_ms > LATENCY_SLO_MAX_MS
    return {
        "slo": "latency", "cohort": cohort, "p95_ms": p95_ms,
        "threshold_ms": LATENCY_SLO_MAX_MS, "breached": breached,
        "headline": (f"agent.invoke p95 = {p95_ms} ms vs SLO max {LATENCY_SLO_MAX_MS} ms "
                     f"in cohort '{cohort}'."),
    }


def wait_for_cohort(mcp, cohort, min_calls, timeout_s=120, poll_s=6):
    """Poll SigNoz until a cohort's telemetry has landed (ingestion lag), so the
    detector reads a complete rollout rather than a half-ingested one."""
    base = (f"service.name = '{TARGET_SERVICE}' AND experiment.id = '{cohort}' "
            f"AND name = 'llm.chat'")
    deadline = time.time() + timeout_s
    n = 0
    while time.time() < deadline:
        n = _count(mcp, base, "20m")
        if n >= min_calls:
            return n
        time.sleep(poll_s)
    return n
