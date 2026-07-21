"""Run the MCP reliability certification suite and publish the verdict to SigNoz.

This is the "observability as tests" entry point. It observes a live MCP server
(``mcp2_probe``), judges it against the deterministic contracts
(``mcp2_contracts``), and emits the whole verdict to SigNoz as all three
OpenTelemetry signals on the ``mcp-contract-lab`` service:

  * TRACES  -- one ``mcp.cert.suite`` root span, a child ``cert.<contract>`` span
    per contract (ERROR-flagged on BREACH), and, underneath, the real
    instrumented ``mcp.*`` client spans of every probe call.
  * METRICS -- ``mcp.cert.contract`` (PASS/BREACH/UNKNOWN per contract),
    ``mcp.cert.grade`` (the overall grade), ``mcp.cert.blind`` (coverage gaps),
    and the ``mcp.client.*`` call latency/volume from the probe layer.
  * LOGS    -- a structured, trace-correlated ``mcp2.*`` line per contract, so the
    verdict is queryable as logs too.

Because detection is deterministic and fail-closed, the suite doubles as a CI
gate: it exits non-zero when any contract BREACHES or the server cannot be
certified at all.

    python mcp2_cert.py                              # certify localhost SigNoz MCP
    python mcp2_cert.py --pin-baseline               # pin the drift baseline
    python mcp2_cert.py --fault corrupt:signoz_list_alert_rules   # inject a fault
    python mcp2_cert.py --url http://host/mcp --no-export --json out.json
"""
import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("OTEL_SERVICE_NAME", "mcp-contract-lab")

import telemetry
import mcp2_metrics
import mcp2_probe
import mcp2_contracts as C
from mcp2_model import ToolInfo, BREACH, UNKNOWN, FAILED, BLIND, CERTIFIED

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "mcp2_baseline.json"
DEFAULT_URL = os.getenv("SIGNOZ_MCP_URL", "http://localhost:8000/mcp")
DEFAULT_SLO_MS = float(os.getenv("MCP2_LATENCY_SLO_MS", str(C.DEFAULT_LATENCY_SLO_MS)))

tracer = trace.get_tracer("mcp-contract-lab")
log = logging.getLogger("mcp-contract-lab")


def _clog(event, level=logging.INFO, **attrs):
    """Structured, trace-correlated log line for one certification event, mapped to
    dotted ``mcp2.*`` attributes so it is queryable in SigNoz alongside the trace
    and metrics of the same run."""
    fields = " ".join(f"{k}={v}" for k, v in attrs.items())
    extra = {"mcp2.event": event}
    for k, v in attrs.items():
        key = k if k.startswith("mcp2.") else "mcp2." + k
        extra[key] = v if isinstance(v, (str, bool, int, float)) else str(v)
    log.log(level, "mcp2.%s %s", event, fields, extra=extra)


# --- drift baseline -----------------------------------------------------------
def load_baseline():
    if not BASELINE.exists():
        return None
    try:
        d = json.loads(BASELINE.read_text())
        return [ToolInfo(**t) for t in d.get("tools", [])]
    except Exception:  # noqa: BLE001
        return None


def pin_baseline(obs):
    payload = {
        "server": obs.server_name, "url": obs.url,
        "protocol_version": obs.protocol_version,
        "fingerprint": C.catalog_fingerprint(obs.tools),
        "captured_at": obs.captured_at,
        "tools": [{"name": t.name, "input_schema": t.input_schema,
                   "description": t.description} for t in obs.tools],
    }
    BASELINE.write_text(json.dumps(payload, indent=2))
    print(f"pinned drift baseline: {len(obs.tools)} tools, "
          f"fingerprint {payload['fingerprint'][:16]} -> {BASELINE.name}")


# --- scoreboard ---------------------------------------------------------------
def scoreboard(report):
    c = report.counts
    print("\n" + "=" * 72)
    print("  MCP CONTRACT LAB  --  reliability certification")
    print("=" * 72)
    print(f"  server:      {report.url}")
    if report.fault_injected:
        print(f"  fault:       {report.fault_injected}  (injected chaos)")
    print(f"  fingerprint: {report.fingerprint[:16] or '(none)'}")
    print(f"  contracts:   {c['PASS']} pass, {c['BREACH']} breach, {c['UNKNOWN']} unknown")
    print("-" * 72)
    for v in report.verdicts:
        mark = {"PASS": "ok  ", "BREACH": "XX  ", "UNKNOWN": "??  "}[v.status]
        print(f"  {mark}[{v.status:7}] {v.contract:26} {v.reason}")
    print("-" * 72)
    print(f"  GRADE: {report.grade}")
    print("=" * 72 + "\n")


# --- emit to SigNoz -----------------------------------------------------------
def emit(report, export):
    if not export:
        return None
    with tracer.start_as_current_span("mcp.cert.suite", kind=SpanKind.INTERNAL) as root:
        trace_hex = format(root.get_span_context().trace_id, "032x")
        root.set_attribute("mcp.server", report.url)
        root.set_attribute("mcp.cert.grade", report.grade)
        root.set_attribute("mcp.cert.fingerprint", report.fingerprint)
        root.set_attribute("mcp.cert.fault", report.fault_injected or "none")
        for key, val in report.counts.items():
            root.set_attribute(f"mcp.cert.{key.lower()}", val)
        _clog("suite.start", server=report.url, grade=report.grade,
              fault=report.fault_injected or "none")
        for v in report.verdicts:
            with tracer.start_as_current_span(f"cert.{v.contract}",
                                              kind=SpanKind.INTERNAL) as cs:
                cs.set_attribute("mcp2.contract", v.contract)
                cs.set_attribute("mcp2.status", v.status)
                cs.set_attribute("mcp2.reason", v.reason)
                if v.evidence:
                    cs.set_attribute("mcp2.evidence", json.dumps(v.evidence)[:500])
                if v.status == BREACH:
                    cs.set_status(Status(StatusCode.ERROR, v.reason))
            mcp2_metrics.contract(v.contract, v.status, report.url)
            lvl = logging.ERROR if v.status == BREACH else (
                logging.WARNING if v.status == UNKNOWN else logging.INFO)
            _clog("contract", level=lvl, contract=v.contract, status=v.status,
                  reason=v.reason)
        mcp2_metrics.grade(report.grade, report.url)
        _clog("suite.done", grade=report.grade, **{k.lower(): val
              for k, val in report.counts.items()})
        if report.grade == FAILED:
            root.set_status(Status(StatusCode.ERROR,
                                   f"{report.counts[BREACH]} contract(s) breached"))
    return trace_hex


def run(url, fault, read_tools, latency_slo_ms, pin, export, json_out):
    if export:
        telemetry.setup_telemetry()
        mcp2_metrics.init()

    print(f"observing MCP server {url} ...")
    obs = mcp2_probe.observe(url, fault=fault, read_tools=read_tools)
    if not obs.reachable:
        print(f"  server unreachable: "
              f"{obs.probes[-1].note if obs.probes else 'no response'}")

    if pin:
        if not obs.reachable or not obs.tools:
            print("cannot pin a baseline: no tool catalog observed.")
            return 1
        pin_baseline(obs)

    cfg = C.CertConfig(latency_slo_ms=latency_slo_ms, baseline_tools=load_baseline())
    report = C.certify(obs, cfg)

    scoreboard(report)
    trace_hex = emit(report, export)

    out = json_out or (HERE / "mcp2_report.json")
    Path(out).write_text(json.dumps({**report.to_dict(),
                                     "captured_at": obs.captured_at,
                                     "trace": trace_hex}, indent=2))
    print(f"report -> {out}")

    if export:
        telemetry.shutdown()
        if trace_hex:
            ui = os.getenv("SIGNOZ_UI", "http://localhost:8080")
            print(f"  cert suite trace: {trace_hex}")
            print(f"  view:             {ui}/trace/{trace_hex}")

    return 1 if report.grade in (FAILED, BLIND) else 0


def main():
    ap = argparse.ArgumentParser(description="MCP reliability certification (SigNoz-native)")
    ap.add_argument("--url", default=DEFAULT_URL, help="MCP server streamable-HTTP endpoint")
    ap.add_argument("--fault", default="", help="inject chaos: latency:TOOL:MS | corrupt:TOOL | drop:TOOL")
    ap.add_argument("--read-tool", action="append", dest="read_tools", default=None,
                    help="pin an explicit read tool to sample (repeatable); default is discovery")
    ap.add_argument("--latency-slo-ms", type=float, default=DEFAULT_SLO_MS,
                    help=f"read-call p95 latency SLO (default {DEFAULT_SLO_MS:.0f} ms)")
    ap.add_argument("--pin-baseline", action="store_true",
                    help="capture the current tool catalog as the drift baseline and exit-code as normal")
    ap.add_argument("--no-export", action="store_true", help="skip SigNoz export (offline scoreboard only)")
    ap.add_argument("--json", dest="json_out", default=None, help="write the JSON report here")
    args = ap.parse_args()
    code = run(args.url, args.fault, args.read_tools, args.latency_slo_ms,
               args.pin_baseline, not args.no_export, args.json_out)
    sys.exit(code)


if __name__ == "__main__":
    main()
