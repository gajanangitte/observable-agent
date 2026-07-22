"""Curated self-observability tools for the introspection agent.

Each tool the model sees maps to a real SigNoz MCP `tools/call`, issued under
its own ``mcp.signoz_*`` span so the self-debugging session is itself a trace
you can open in SigNoz. The 41 raw MCP tools take rich, free-form arguments
(nanosecond durations, filter expressions, groupBy keys) that a 3B CPU model
fills in unreliably, so we expose three *argument-free* tools that pin the
correct arguments and trim the (large) JSON responses down to the few fields
the model actually needs to reason about.
"""
import json

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

import config

tracer = trace.get_tracer("observable-agent")

SERVICE = config.SERVICE_NAME  # "observable-agent" -- the agent observes itself
_NS_PER_MS = 1_000_000

# Span names that exist ONLY because of the introspection itself: the root
# agent.introspect span, the mcp.* calls it makes, and the tool.* wrappers whose
# latency is dominated by the SigNoz round-trip. Excluding them lets the agent
# reason about its real request-serving workload (llm.chat vs the fast SRE
# tools) instead of measuring the observer.
_SCAFFOLD_SPANS = {
    "agent.introspect",
    "tool.list_my_services",
    "tool.latency_by_operation",
    "tool.find_slowest_traces",
}


def _is_scaffold(op):
    return op in _SCAFFOLD_SPANS or op.startswith("mcp.")


def _ms(nanos):
    if nanos is None:
        return None
    return round(float(nanos) / _NS_PER_MS, 1)


def _mcp_call(mcp, tool_name, args):
    """Issue one real MCP tools/call, wrapped in an mcp.<tool> CLIENT span."""
    with tracer.start_as_current_span(f"mcp.{tool_name}", kind=SpanKind.CLIENT) as span:
        span.set_attribute("mcp.server.url", mcp.url)
        span.set_attribute("mcp.transport", "streamable_http")
        span.set_attribute("mcp.method", "tools/call")
        span.set_attribute("mcp.tool.name", tool_name)
        span.set_attribute("mcp.request.args", json.dumps(args)[:500])
        try:
            raw = mcp.call_tool(tool_name, args)
        except Exception as e:
            # call_tool RAISES on an MCP-side error (isError) or transport failure.
            # Introspection is a best-effort read, not a heal decision, so surface the
            # failure as a graceful error dict rather than crashing the whole session.
            span.set_status(Status(StatusCode.ERROR, f"MCP call failed: {e}"))
            return {"error": f"MCP call failed: {e}", "tool": tool_name}
        span.set_attribute("mcp.response.bytes", len(raw or ""))
        # Some SigNoz tools append a plain-text pagination hint after the JSON
        # body ("note: returned N rows ..."), so decode just the first JSON value
        # rather than json.loads-ing the whole string.
        try:
            parsed, _ = json.JSONDecoder().raw_decode((raw or "").lstrip())
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, f"non-JSON MCP response: {e}"))
            return {"error": "MCP server returned non-JSON", "raw": (raw or "")[:200]}
        return parsed


def build(mcp):
    """Return (tool_schemas, registry) bound to a live SigNoz MCP client."""

    def list_my_services(**_):
        """MCP: signoz_list_services -> the agent's own service summary."""
        parsed = _mcp_call(mcp, "signoz_list_services",
                            {"timeRange": "6h", "limit": "50",
                             "searchContext": "the agent inspecting its own service health"})
        services = parsed.get("data") or []
        out = []
        for s in services:
            out.append({
                "service": s.get("serviceName"),
                "num_calls": s.get("numCalls"),
                "error_rate": s.get("errorRate"),
                "avg_duration_ms": _ms(s.get("avgDuration")),
                "p99_ms": _ms(s.get("p99")),
            })
        return {"services": out}

    def latency_by_operation(**_):
        """MCP: signoz_aggregate_traces -> p95 latency per operation (span name)."""
        parsed = _mcp_call(mcp, "signoz_aggregate_traces", {
            "aggregation": "p95",
            "aggregateOn": "duration_nano",
            "groupBy": "name",
            "filter": f"service.name = '{SERVICE}'",
            "timeRange": "6h",
            "limit": "20",
        })
        rows = (((parsed.get("data") or {}).get("data") or {}).get("results") or [{}])[0].get("data") or []
        ops = []
        for row in rows:
            if not row or len(row) < 2 or row[1] is None:
                continue
            op = row[0]
            # Hide the introspection scaffolding itself (the agent.introspect root,
            # the mcp.* calls, and the telemetry-reading tool wrappers) so the model
            # reasons about its request-serving work, not the observer overhead.
            if _is_scaffold(op):
                continue
            ops.append({"operation": op, "p95_ms": _ms(row[1])})
        ops.sort(key=lambda o: o["p95_ms"], reverse=True)
        # Ground the (small) model with the exact comparison so it relays real
        # numbers instead of hallucinating plausible ones. The percentile math is
        # SigNoz's job; the model's job is to orchestrate and interpret.
        llm = next((o for o in ops if o["operation"] == "llm.chat"), None)
        tool_ops = [o for o in ops if o["operation"].startswith("tool.") and o["p95_ms"]]
        slowest_tool = max(tool_ops, key=lambda o: o["p95_ms"], default=None)
        headline = None
        if llm and slowest_tool:
            ratio = round(llm["p95_ms"] / slowest_tool["p95_ms"])
            headline = (
                f"llm.chat p95 = {llm['p95_ms']} ms; slowest tool "
                f"({slowest_tool['operation']}) p95 = {slowest_tool['p95_ms']} ms; "
                f"the model call is ~{ratio}x slower than the tool call."
            )
        return {"headline": headline, "operations": ops}

    def find_slowest_traces(**_):
        """MCP: signoz_search_traces -> the agent's slowest recent requests."""
        parsed = _mcp_call(mcp, "signoz_search_traces", {
            "service": SERVICE,
            "operation": "agent.invoke",
            "minDuration": "1",
            "timeRange": "6h",
            "limit": "10",
            "searchContext": "find my slowest recent requests",
        })
        results = (((parsed.get("data") or {}).get("data") or {}).get("results") or [{}])
        rows = results[0].get("rows") or [] if results else []
        traces = []
        for r in rows:
            d = r.get("data") or {}
            traces.append({
                "trace_id": d.get("trace_id"),
                "duration_ms": _ms(d.get("duration_nano")),
                "had_error": d.get("has_error"),
            })
        traces = [t for t in traces if t["duration_ms"] is not None]
        traces.sort(key=lambda t: t["duration_ms"], reverse=True)
        return {"slowest_traces": traces[:5]}

    registry = {
        "list_my_services": list_my_services,
        "latency_by_operation": latency_by_operation,
        "find_slowest_traces": find_slowest_traces,
    }

    schemas = [
        {
            "type": "function",
            "function": {
                "name": "list_my_services",
                "description": ("Return your own service's traffic summary from SigNoz "
                                "(call count, error rate, average and p99 latency). Takes no arguments."),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "latency_by_operation",
                "description": ("Return your p95 latency (in milliseconds) broken down by operation "
                                "-- e.g. agent.invoke, llm.chat, and each tool.* call. Use this to see "
                                "which operation dominates your latency. Takes no arguments."),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_slowest_traces",
                "description": ("Return your slowest recent requests (trace ids and their total "
                                "duration in milliseconds). Takes no arguments."),
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]
    return schemas, registry
