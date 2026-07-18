"""Self-Healing SRE Sidekick -- the closed loop, with SigNoz as the sensor.

SigNoz is both the loop's SENSOR and its SCOREBOARD:

  agent.heal (SERVER, service=self-healer)
    |-- heal.canary.pre     break it: roll out the workload under the injected fault
    |-- heal.detect         MCP: is the retry-tax SLO breached for the pre cohort?
    |-- heal.decide         local qwen2.5 reads the incident via MCP, picks a fix
    |     |-- llm.chat
    |     |-- tool.read_incident --> mcp.signoz_aggregate_traces
    |     |-- llm.chat
    |     |-- tool.disable_fault_injection   (the ACT: a control-plane change)
    |-- heal.canary.post    verify: roll out again under the fixed config
    |-- heal.verify         MCP: is the retry rate back to zero? -> healed; record MTTR

The managed workload (observable-agent) runs as canary SUBPROCESSES so each
rollout picks up the new config fresh -- a real rollout, not an in-process
monkey-patch. Detection and verification are deterministic MCP queries; the
local model is only asked to *decide*.
"""
import os

# The healer is its own service in SigNoz, and it decides with a model that does
# reliable tool-calling on Ollama. Set before importing config (read at import).
os.environ.setdefault("OTEL_SERVICE_NAME", "self-healer")
os.environ.setdefault("AGENT_MODEL", "qwen2.5:3b")
os.environ.setdefault("AGENT_MAX_OUTPUT_TOKENS", "300")   # room for decision + tool call
os.environ.setdefault("CANARY_QUESTIONS", "2")            # 2 rollout requests / cohort

import subprocess
import sys
import time

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

import config
import telemetry
import heal_metrics
import heal_sensors
import heal_actuators
from heal_controls import Controls
from mcp_client import SigNozMCP
from agent import Agent

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNOZ_UI = os.getenv("SIGNOZ_UI", "http://localhost:8080")
tracer = trace.get_tracer("self-healer")

HEAL_SYSTEM = (
    "You are a self-healing SRE agent that works in two steps. "
    "STEP 1: call read_incident to fetch the breach evidence from SigNoz. "
    "STEP 2: based on that evidence, call exactly ONE remediation tool to fix the "
    "root cause, then stop. read_incident only READS data -- it does not fix "
    "anything -- so you MUST follow it with a remediation tool call. Do not write "
    "a prose answer until a remediation tool has been called. Never invent numbers."
)
HEAL_TASK = (
    "A retry-tax reliability SLO was breached in rollout cohort '{cohort}': LLM "
    "responses are being dropped and retried, wasting tokens. First call "
    "read_incident to confirm the evidence, then call disable_fault_injection to "
    "remove the fault at its source (or enable_mitigation to compensate for it). "
    "Remediate now."
)

MAX_HEAL_ATTEMPTS = 2


def _banner(txt):
    print("\n" + "=" * 68 + f"\n  {txt}\n" + "=" * 68)


def _run_canary(controls, cohort, span_name):
    """Roll out the managed workload once, tagged with `cohort`, as a subprocess."""
    with tracer.start_as_current_span(span_name, kind=SpanKind.INTERNAL) as span:
        env = controls.canary_env(cohort)
        fault = env["CHAOS_DROP_RESPONSE_ONCE"] == "1"
        span.set_attribute("canary.cohort", cohort)
        span.set_attribute("canary.model", env["AGENT_MODEL"])
        span.set_attribute("canary.fault_injected", fault)
        print(f"  -> rolling out '{cohort}' (model={env['AGENT_MODEL']}, fault={fault})")
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "heal_canary.py")],
            env=env, cwd=HERE, capture_output=True, text=True, timeout=900)
        for line in (proc.stdout or "").splitlines():
            print("    " + line)
        if proc.returncode != 0:
            span.set_status(Status(StatusCode.ERROR, "canary rollout failed"))
            print("    canary stderr:", (proc.stderr or "")[:300])


def main():
    telemetry.setup_telemetry()
    heal_metrics.init()
    mcp = SigNozMCP(config.MCP_URL)
    controls = Controls()
    controls.reset(broken=True)   # start sick: response-drop fault injected

    cycle = time.strftime("%H%M%S")
    pre = f"heal-{cycle}-pre"
    _banner("SELF-HEALING SRE SIDEKICK   (SigNoz = sensor + scoreboard)")

    after = None
    chosen = None
    with tracer.start_as_current_span("agent.heal", kind=SpanKind.SERVER) as root:
        trace_id_hex = format(root.get_span_context().trace_id, "032x")
        root.set_attribute("heal.cycle", cycle)
        root.set_attribute("service.managed", heal_sensors.TARGET_SERVICE)

        # ---- BREAK IT: a rollout under the injected fault ------------------
        print("\n[SETUP]  managed workload is sick: response-drop fault is injected.")
        _run_canary(controls, pre, "heal.canary.pre")

        # ---- DETECT -------------------------------------------------------
        print("\n[DETECT] asking SigNoz (via MCP) whether the rollout breached its SLO...")
        with tracer.start_as_current_span("heal.detect", kind=SpanKind.INTERNAL) as ds:
            heal_sensors.wait_for_cohort(mcp, pre, min_calls=2)
            slo = heal_sensors.retry_slo(mcp, pre)
            ds.set_attribute("slo.name", slo["slo"])
            ds.set_attribute("slo.retry_rate", slo["retry_rate"])
            ds.set_attribute("slo.dropped", slo["dropped"])
            ds.set_attribute("slo.breached", slo["breached"])
            print("  " + slo["headline"])
        if not slo["breached"]:
            print("  no breach detected -- nothing to heal. (Is the fault firing?)")
            root.set_status(Status(StatusCode.OK))
            telemetry.shutdown()
            return
        heal_metrics.breach(slo["slo"], pre)
        heal_metrics.retry_rate(slo["retry_rate"], pre, "pre")
        root.set_attribute("heal.breach.retry_rate", slo["retry_rate"])
        t_breach = time.time()

        healed = False
        attempt = 0
        actions_left = ["disable_fault_injection", "enable_mitigation"]
        while not healed and attempt < MAX_HEAL_ATTEMPTS and actions_left:
            attempt += 1

            # ---- DIAGNOSE + DECIDE (the agentic step, via MCP) ------------
            print(f"\n[DIAGNOSE] attempt {attempt}: local {config.MODEL} reads the incident "
                  f"via MCP and decides on a fix...")
            decisions = []
            schemas, registry = heal_actuators.build(
                mcp, controls, pre, decisions, actions=tuple(actions_left))
            healer = Agent(tool_schemas=schemas, registry=registry,
                           system_prompt=HEAL_SYSTEM, root_span="heal.decide",
                           temperature=0.0)
            try:
                healer.invoke(HEAL_TASK.format(cohort=pre))
            except Exception as e:  # noqa: BLE001
                print("  decide step raised:", e)
            chosen = next((d for d in decisions if d != "read_incident"), None)
            if not chosen:
                # Safety net: the model read the incident but didn't act. Apply the
                # root-cause fix ourselves, still under a span so the trace records it.
                chosen = "disable_fault_injection"
                with tracer.start_as_current_span(f"tool.{chosen}", kind=SpanKind.INTERNAL) as fs:
                    fs.set_attribute("tool.name", chosen)
                    fs.set_attribute("heal.fallback", True)
                    registry[chosen]()
                print("  (model did not act; applied safe default remediation)")
            print(f"[ACT]     remediation applied: {chosen}")
            heal_metrics.action(chosen)
            root.set_attribute(f"heal.action.{attempt}", chosen)
            actions_left = [a for a in actions_left if a != chosen]

            # ---- VERIFY: a rollout under the fixed config ----------------
            post = f"heal-{cycle}-post{attempt}"
            print(f"\n[VERIFY]  rolling out '{post}' under the fixed config, then "
                  f"re-checking SigNoz...")
            _run_canary(controls, post, "heal.canary.post")
            with tracer.start_as_current_span("heal.verify", kind=SpanKind.INTERNAL) as vs:
                heal_sensors.wait_for_cohort(mcp, post, min_calls=2)
                after = heal_sensors.retry_slo(mcp, post)
                vs.set_attribute("slo.retry_rate", after["retry_rate"])
                vs.set_attribute("slo.breached", after["breached"])
                print("  " + after["headline"])
            healed = not after["breached"]
            heal_metrics.retry_rate(after["retry_rate"], post, "post")

        # ---- OUTCOME ------------------------------------------------------
        mttr_ms = (time.time() - t_breach) * 1000
        heal_metrics.result(slo["slo"], healed)
        heal_metrics.mttr(mttr_ms, slo["slo"])
        root.set_attribute("heal.healed", healed)
        root.set_attribute("heal.mttr_ms", round(mttr_ms))
        root.set_status(Status(StatusCode.OK))

        _banner("HEALED" if healed else "NOT HEALED -- escalate")
        print(f"  retry rate:  {slo['retry_rate']:.0%}  ->  "
              f"{after['retry_rate']:.0%}")
        print(f"  remediation: {chosen}")
        print(f"  MTTR:        {mttr_ms / 1000:.0f}s   (breach detected -> verified healed)")
        print(f"  trace:       agent.heal   {trace_id_hex}")
        print(f"  view:        {SIGNOZ_UI}/trace/{trace_id_hex}")

    telemetry.shutdown()
    print("\nflushed self-healer traces + metrics to SigNoz.")


if __name__ == "__main__":
    main()
