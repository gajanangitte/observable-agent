"""OpenTelemetry instruments for the MCP Contract Lab.

Created lazily via :func:`init` AFTER ``telemetry.setup_telemetry`` has installed
the global MeterProvider, exactly like ``heal_metrics``. Every recorder is a safe
no-op until ``init`` runs, so the probe layer can also be used fully offline (no
SigNoz) and simply emits nothing.

Two families of signal:
  * mcp.client.*  -- one measurement per instrumented MCP interaction (the
    auto-instrumentation layer): call latency, call count, split by tool / method
    / status. This is what lights up a "is my MCP server healthy" dashboard.
  * mcp.cert.*    -- the certification verdicts: one point per contract per run
    (PASS / BREACH / UNKNOWN), the overall grade, and a dedicated blind-spot
    counter for UNKNOWN contracts (the checks the lab could not evaluate).
"""
from opentelemetry import metrics

_m = {}


def init():
    meter = metrics.get_meter("mcp2-contract-lab")
    _m["call_dur"] = meter.create_histogram(
        "mcp.client.call.duration", unit="ms",
        description="Latency of one instrumented MCP call (tools/call, tools/list, ...)")
    _m["calls"] = meter.create_counter(
        "mcp.client.calls", unit="{call}",
        description="Instrumented MCP calls, labelled by tool, method and status")
    _m["contract"] = meter.create_counter(
        "mcp.cert.contract", unit="{verdict}",
        description="MCP reliability contract verdicts (status = PASS|BREACH|UNKNOWN)")
    _m["grade"] = meter.create_counter(
        "mcp.cert.grade", unit="{run}",
        description="Overall certification grade per suite run")
    _m["blind"] = meter.create_counter(
        "mcp.cert.blind", unit="{contract}",
        description="Contracts the lab could NOT evaluate (UNKNOWN): coverage blind spots")


def client_call(tool, method, status, error_class, latency_ms):
    if not _m:
        return
    attrs = {"mcp.tool.name": tool, "mcp.method": method, "status": status,
             "mcp.error.class": error_class}
    _m["calls"].add(1, attrs)
    if latency_ms is not None:
        _m["call_dur"].record(latency_ms, attrs)


def contract(name, status, server):
    if not _m:
        return
    _m["contract"].add(1, {"contract": name, "status": status, "mcp.server": server})
    if status == "UNKNOWN":
        _m["blind"].add(1, {"contract": name, "mcp.server": server})


def grade(value, server):
    if not _m:
        return
    _m["grade"].add(1, {"grade": value, "mcp.server": server})
