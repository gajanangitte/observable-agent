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

import heal_baseline
import heal_fingerprint
import heal_stats
import economics

TARGET_SERVICE = "observable-agent"   # the managed workload the healer watches
RETRY_SLO_MAX_RATE = 0.05             # > 5% dropped-and-retried llm.chat = breach
LATENCY_SLO_MAX_MS = 60000            # agent.invoke p95 must stay under 60s
# Cost SLO + nominal per-call cost are sourced from the economics model (defaults
# 6 and 0.00004), so a company retunes "too expensive" in economics.yaml without
# editing this file. The count stays an int for a clean headline when it is whole.
_cpr = economics.cost_slo_max_calls_per_request()
COST_SLO_MAX_CALLS_PER_REQ = int(_cpr) if float(_cpr).is_integer() else _cpr
NOMINAL_CALL_COST_USD = economics.nominal_call_cost_usd()   # fallback per-call spend

# Every sensor reads one of three states. The critical safety property is that a
# blind sensor reports UNKNOWN -- it NEVER silently reports 0 / healthy when it
# cannot see (MCP down, or no telemetry yet). The healer refuses to act on, or to
# declare an incident healed from, an UNKNOWN reading.
STATUS_PASS = "PASS"
STATUS_BREACH = "BREACH"
STATUS_UNKNOWN = "UNKNOWN"

# Sentinel returned by _agg when the MCP query itself FAILED (transport / non-JSON
# / empty), so callers can tell "the query failed" apart from "the query ran and
# genuinely counted zero" -- the distinction the old fail-open code collapsed.
_FAILED = object()

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
        try:
            raw = mcp.call_tool("signoz_aggregate_traces", args)
        except Exception as e:  # noqa: BLE001
            span.set_attribute("mcp.ok", False)
            span.set_status(Status(StatusCode.ERROR, f"MCP call failed: {e}"))
            return _FAILED
        span.set_attribute("mcp.response.bytes", len(raw or ""))
        if not raw:
            span.set_attribute("mcp.ok", False)
            span.set_status(Status(StatusCode.ERROR, "empty MCP response"))
            return _FAILED
        # SigNoz sometimes appends a plain-text pagination note after the JSON,
        # so decode just the first JSON value rather than json.loads-ing it all.
        try:
            parsed, _ = json.JSONDecoder().raw_decode(raw.lstrip())
        except Exception as e:  # noqa: BLE001
            span.set_attribute("mcp.ok", False)
            span.set_status(Status(StatusCode.ERROR, f"non-JSON MCP response: {e}"))
            return _FAILED
        span.set_attribute("mcp.ok", True)
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
    if parsed is _FAILED:
        return None                       # query FAILED -> unknown, never a silent 0
    val = _scalar(parsed)
    return int(val) if val is not None else 0


def _sum(mcp, attr, filter_expr, time_range="20m"):
    """Best-effort sum of a numeric span attribute (None if not aggregatable)."""
    parsed = _agg(mcp, {
        "aggregation": "sum", "aggregateOn": attr,
        "filter": filter_expr, "timeRange": time_range, "limit": "5",
    })
    if parsed is _FAILED:
        return None
    return _scalar(parsed)


def _unknown(slo, cohort, reason, retryable):
    """A sensor that cannot see: MCP down (retryable=False) or no telemetry to
    judge yet (retryable=True, i.e. ingestion lag). The healer must never act on,
    or declare healthy from, an UNKNOWN reading."""
    return {
        "slo": slo, "cohort": cohort, "status": STATUS_UNKNOWN, "known": False,
        "breached": False, "hard_breach": False, "anomaly_only": False,
        "reason": reason, "retryable": retryable,
        "headline": f"SLO '{slo}' is UNKNOWN for cohort '{cohort}': {reason}.",
    }


def _classify(slo_name, observed, hard_breach):
    """Fold the fixed-floor result together with the robust-stats supplement.

    Returns (status, anomaly_only, stats). A statistical anomaly that is NOT also
    a fixed-floor breach is flagged ``anomaly_only`` so the loop can propose it
    (lower autonomy) rather than auto-apply. Only a HEALTHY (PASS) reading is fed
    back into the baseline, so an incident can't poison its own reference."""
    baseline = heal_baseline.series(slo_name)
    stats = heal_stats.assess(baseline, observed)
    status = STATUS_BREACH if (hard_breach or stats["anomaly"]) else STATUS_PASS
    if status == STATUS_PASS:
        heal_baseline.append(slo_name, observed)
    return status, (status == STATUS_BREACH and not hard_breach), stats


def retry_slo(mcp, cohort, time_range="20m"):
    """Retry-tax SLO: fraction of a cohort's llm.chat calls that were dropped
    and retried. This is the healer's primary sensor."""
    base = (f"service.name = '{TARGET_SERVICE}' AND experiment.id = '{cohort}' "
            f"AND name = 'llm.chat'")
    total = _count(mcp, base, time_range)
    dropped = _count(mcp, base + " AND retry.reason = 'response_dropped'", time_range)
    if total is None or dropped is None:
        return _unknown("retry_tax", cohort,
                        "SigNoz query failed (MCP unreachable or errored)", retryable=False)
    if total == 0:
        return _unknown("retry_tax", cohort,
                        "no llm.chat calls observed yet for this cohort", retryable=True)
    rate = dropped / total
    hard_breach = rate > RETRY_SLO_MAX_RATE
    status, anomaly_only, stats = _classify("retry_tax", rate, hard_breach)
    fp = heal_fingerprint.fingerprint(
        {"slo": "retry_tax", "retry_rate": rate, "threshold": RETRY_SLO_MAX_RATE})
    note = " [statistical anomaly vs baseline, not a fixed-floor breach]" if anomaly_only else ""
    return {
        "slo": "retry_tax", "cohort": cohort, "status": status, "known": True,
        "total_llm_calls": total, "dropped": dropped, "retry_rate": round(rate, 3),
        "threshold": RETRY_SLO_MAX_RATE,
        "breached": status == STATUS_BREACH, "hard_breach": hard_breach,
        "anomaly_only": anomaly_only,
        "baseline": stats["baseline"], "sigma": stats["sigma"],
        "fingerprint": fp.as_dict(),
        "headline": (f"{dropped}/{total} llm.chat calls were dropped and retried "
                     f"(retry rate {rate:.0%} vs SLO max {RETRY_SLO_MAX_RATE:.0%}) "
                     f"in cohort '{cohort}'{note}."),
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
    if calls is None or reqs is None:
        return _unknown("cost_runaway", cohort,
                        "SigNoz query failed (MCP unreachable or errored)", retryable=False)
    if reqs == 0:
        return _unknown("cost_runaway", cohort,
                        "no agent.invoke requests observed yet for this cohort", retryable=True)
    cpr = calls / reqs
    spent = _sum(mcp, "gen_ai.usage.cost_usd", svc + " AND name = 'llm.chat'", time_range)
    if spent is None:
        spent = calls * NOMINAL_CALL_COST_USD
    per_req_spend = spent / reqs
    hard_breach = cpr > COST_SLO_MAX_CALLS_PER_REQ
    status, anomaly_only, stats = _classify("cost_runaway", cpr, hard_breach)
    fp = heal_fingerprint.fingerprint(
        {"slo": "cost_runaway", "calls_per_request": cpr,
         "threshold_calls": COST_SLO_MAX_CALLS_PER_REQ})
    note = " [statistical anomaly vs baseline, not a fixed-floor breach]" if anomaly_only else ""
    return {
        "slo": "cost_runaway", "cohort": cohort, "status": status, "known": True,
        "requests": reqs, "llm_calls": calls,
        "calls_per_request": round(cpr, 2),
        "spend_usd": round(spent, 6),
        "spend_per_request_usd": round(per_req_spend, 6),
        "threshold_calls": COST_SLO_MAX_CALLS_PER_REQ,
        "breached": status == STATUS_BREACH, "hard_breach": hard_breach,
        "anomaly_only": anomaly_only,
        "baseline": stats["baseline"], "sigma": stats["sigma"],
        "fingerprint": fp.as_dict(),
        "headline": (f"{calls} llm.chat calls across {reqs} request(s) = "
                     f"{cpr:.1f}/request (~${spent:.6f} spend) vs SLO max "
                     f"{COST_SLO_MAX_CALLS_PER_REQ}/request in cohort '{cohort}'{note}."),
    }


def latency_slo(mcp, cohort, time_range="20m"):
    """Latency SLO: agent.invoke p95 for a cohort. Secondary sensor (model swap)."""
    parsed = _agg(mcp, {
        "aggregation": "p95", "aggregateOn": "duration_nano", "groupBy": "name",
        "filter": (f"service.name = '{TARGET_SERVICE}' AND experiment.id = '{cohort}' "
                   f"AND name = 'agent.invoke'"),
        "timeRange": time_range, "limit": "5",
    })
    if parsed is _FAILED:
        return _unknown("latency", cohort,
                        "SigNoz query failed (MCP unreachable or errored)", retryable=False)
    val = _scalar(parsed)
    if val is None:
        return _unknown("latency", cohort,
                        "no agent.invoke spans observed yet for this cohort", retryable=True)
    p95_ms = round(val / _NS_PER_MS, 1)
    hard_breach = p95_ms > LATENCY_SLO_MAX_MS
    status, anomaly_only, stats = _classify("latency", p95_ms, hard_breach)
    return {
        "slo": "latency", "cohort": cohort, "status": status, "known": True,
        "p95_ms": p95_ms, "threshold_ms": LATENCY_SLO_MAX_MS,
        "breached": status == STATUS_BREACH, "hard_breach": hard_breach,
        "anomaly_only": anomaly_only,
        "baseline": stats["baseline"], "sigma": stats["sigma"],
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
        n = _count(mcp, base, "20m") or 0   # None (query failed) -> keep waiting
        if n >= min_calls:
            return n
        time.sleep(poll_s)
    return n
