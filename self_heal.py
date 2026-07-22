"""Self-Healing SRE Sidekick -- the governed closed loop, SigNoz as the sensor.

SigNoz is both the loop's SENSOR and its SCOREBOARD; every remediation passes a
policy gate before it can act, and a fix that fails to verify is rolled back:

  agent.heal (SERVER, service=self-healer)
    |-- heal.canary.pre     break it: roll out the workload under the injected fault
    |-- heal.detect         MCP: is the SLO breached for the pre cohort?
    |-- heal.decide         local qwen2.5 reads the incident via MCP, picks a fix
    |     |-- llm.chat
    |     |-- tool.read_incident --> mcp.signoz_aggregate_traces
    |     |-- llm.chat
    |     |-- tool.<remediation>   (the ACT: policy-gated control-plane change)
    |-- heal.canary.post    verify: roll out again under the fixed config
    |-- heal.verify         MCP: is the SLO back in bounds? -> healed; record MTTR
    |-- heal.rollback       (only if verify still breached: revert the change)

Three incidents ship here (``--scenario``): ``retry`` heals the retry-tax (a
dropped-and-retried response) with ``disable_fault_injection``; ``cost`` heals a
runaway-spend / bill-shock loop by arming a per-request cost circuit-breaker
(``set_cost_budget``); ``carbon`` heals a GreenOps sustainability breach, where the
SAME wasted retries burn real joules and grams of CO2e that the WattTrace (Track 03)
model prices, surfaced as the share of a cohort's inference energy wasted on retries and
cleared by the same retry mitigation. Every action clears ``heal_policy`` first --
low-risk reversible fixes auto-apply, riskier ones are held for human approval.

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

import argparse
import logging
import subprocess
import sys
import time

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

import config
import economics
import telemetry
import heal_metrics
import heal_sensors
import heal_actuators
import heal_policy
import heal_fingerprint
import heal_memory
from heal_controls import Controls, BROKEN, BROKEN_COST
from mcp_client import SigNozMCP
from agent import Agent

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNOZ_UI = os.getenv("SIGNOZ_UI", "http://localhost:8080")
tracer = trace.get_tracer("self-healer")
log = logging.getLogger("self-healer")


def _hlog(event, level=logging.INFO, **attrs):
    """Emit a structured, trace-correlated log record for a heal lifecycle event.
    telemetry.setup_telemetry() attaches an OTLP handler to the root logger, so
    these ship to SigNoz stamped with the active trace_id/span_id -- the LOGS
    signal for the same incident whose TRACES and METRICS we already emit, all
    pivotable together. Values are kept to primitives so they map cleanly to log
    attributes."""
    fields = " ".join(f"{k}={v}" for k, v in attrs.items())
    extra = {"heal.event": event}
    for k, v in attrs.items():
        key = k if k.startswith("heal.") else "heal." + k
        extra[key] = v if isinstance(v, (str, bool, int, float)) else str(v)
    log.log(level, "heal.%s %s", event, fields, extra=extra)

HEAL_SYSTEM = (
    "You are a self-healing SRE agent. You work in two steps. "
    "STEP 1: call read_incident to pull the breach evidence from SigNoz. "
    "STEP 2: weigh the remediation tools available to you and call EXACTLY ONE "
    "whose effect the evidence shows will clear the breach, then stop. "
    "read_incident only READS data, it does not fix anything, so you MUST follow "
    "it with exactly one remediation tool call. Do not write a prose answer until "
    "a remediation tool has been called. Base your choice only on the evidence and "
    "each tool's stated trade-offs; never invent numbers."
)
HEAL_TASK = (
    "A reliability SLO was breached in rollout cohort '{cohort}'. Investigate the "
    "incident with read_incident, then apply the single remediation best supported "
    "by that evidence to bring the breached metric back within its SLO. Remediate now."
)
HEAL_TASK_COST = (
    "A cost SLO was breached in rollout cohort '{cohort}': spend per request is over "
    "budget. Investigate the incident with read_incident, then apply the single "
    "remediation best supported by that evidence to bring spend back within the SLO. "
    "Remediate now."
)
HEAL_TASK_CARBON = (
    "A GreenOps carbon and energy SLO was breached in rollout cohort '{cohort}': a "
    "large share of the CO2e and joules spent per answer is being wasted on "
    "dropped-and-retried calls that served no answer. Investigate the incident with "
    "read_incident, then apply the single remediation best supported by that evidence "
    "to bring the wasted energy back within the SLO. Remediate now."
)

MAX_HEAL_ATTEMPTS = 2


class Scenario:
    """One incident the healer can chase: how to break it, sense it, and fix it."""

    def __init__(self, name, title, seed, sensor, actions, task, fallback,
                 value_of, record, system=HEAL_SYSTEM, min_calls=2):
        self.name, self.title, self.seed = name, title, seed
        self.sensor, self.actions, self.task = sensor, actions, task
        self.fallback, self.value_of, self.record = fallback, value_of, record
        self.system, self.min_calls = system, min_calls


def _retry_value(slo):
    return ("retry rate", f"{slo['retry_rate']:.0%}", slo["retry_rate"])


def _cost_value(slo):
    return ("spend/request",
            f"${slo['spend_per_request_usd']:.6f} ({slo['calls_per_request']:.1f} calls/req)",
            slo["calls_per_request"])


def _retry_record(slo, cohort, phase):
    heal_metrics.retry_rate(slo["retry_rate"], cohort, phase)


def _cost_record(slo, cohort, phase):
    heal_metrics.cost_spend(slo["spend_per_request_usd"], cohort, phase)
    heal_metrics.calls_per_request(slo["calls_per_request"], cohort, phase)


def _carbon_value(slo):
    return ("carbon/answer",
            f"{slo['grams_per_answer']:.4f} gCO2e ({slo['joules_per_answer']:.0f} J)",
            slo["grams_per_answer"])


def _carbon_record(slo, cohort, phase):
    heal_metrics.carbon_footprint(slo["grams_per_answer"], slo["joules_per_answer"],
                                  cohort, phase)


SCENARIOS = {
    "retry": Scenario(
        name="retry_tax",
        title="SELF-HEALING SRE SIDEKICK   (retry-tax incident)",
        seed=lambda c: c.reset(state=BROKEN),
        sensor=heal_sensors.retry_slo,
        actions=("disable_fault_injection", "enable_mitigation"),
        task=HEAL_TASK, fallback="disable_fault_injection",
        value_of=_retry_value, record=_retry_record),
    "cost": Scenario(
        name="cost_runaway",
        title="SELF-HEALING SRE SIDEKICK   (bill-shock / runaway-spend incident)",
        seed=lambda c: c.reset(state=BROKEN_COST),
        sensor=heal_sensors.cost_slo,
        actions=("set_cost_budget", "switch_model"),
        task=HEAL_TASK_COST, fallback="set_cost_budget",
        value_of=_cost_value, record=_cost_record),
    "carbon": Scenario(
        name="carbon_slo",
        title="SELF-HEALING SRE SIDEKICK   (GreenOps carbon / energy SLO incident)",
        seed=lambda c: c.reset(state=BROKEN),
        sensor=heal_sensors.carbon_slo,
        actions=("disable_fault_injection", "enable_mitigation"),
        task=HEAL_TASK_CARBON, fallback="disable_fault_injection",
        value_of=_carbon_value, record=_carbon_record),
}


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


def _settle_read(sensor, mcp, cohort, min_calls, tries=6, sleep_s=5):
    """Wait for a cohort's telemetry, then read its SLO -- retrying ONLY while the
    sensor is UNKNOWN because ingestion has not caught up (retryable). A sensor
    that is blind because MCP is down (not retryable) returns immediately, so the
    loop can refuse to act rather than spin on a dead backend."""
    heal_sensors.wait_for_cohort(mcp, cohort, min_calls=min_calls)
    slo = sensor(mcp, cohort)
    n = 0
    while slo.get("status") == heal_sensors.STATUS_UNKNOWN and slo.get("retryable") and n < tries:
        time.sleep(sleep_s)
        slo = sensor(mcp, cohort)
        n += 1
    return slo


def _base_action(action):
    """Normalise a chosen action to its registry base name (drop any ':arg')."""
    return (action or "").split(":", 1)[0]


def main():
    ap = argparse.ArgumentParser(description="Self-healing SRE sidekick (SigNoz control loop)")
    ap.add_argument("--scenario", default=os.getenv("HEAL_SCENARIO", "retry"),
                    choices=sorted(SCENARIOS),
                    help="incident to heal: 'retry' (retry tax) or 'cost' (bill-shock)")
    ap.add_argument("--break-only", action="store_true",
                    help="seed the fault and emit one breaching rollout for SigNoz to "
                         "alert on, then exit (no heal). Arms the alert-triggered demo.")
    ap.add_argument("--no-seed", action="store_true",
                    help="heal the current (already-sick) control-plane state without "
                         "re-seeding the fault, e.g. when a SigNoz alert triggered this run.")
    ap.add_argument("--triggered-by", default=os.getenv("HEAL_TRIGGER_ALERT"),
                    help="name/id of the SigNoz alert that woke the healer; stamped on the "
                         "agent.heal trace so the alert and the heal share one story.")
    args = ap.parse_args()
    scenario = SCENARIOS[args.scenario]

    telemetry.setup_telemetry()
    heal_metrics.init()
    mcp = SigNozMCP(config.MCP_URL)
    controls = Controls()
    if not args.no_seed:
        scenario.seed(controls)   # start sick, in this incident's broken state
    policy = heal_policy.Policy()  # the governance gate every action passes through

    cycle = time.strftime("%H%M%S")

    # BREAK-ONLY: emit one breaching rollout so a real SigNoz alert can fire, then
    # exit. The alert-triggered heal is launched separately (see heal_bridge.py).
    if args.break_only:
        _banner(scenario.title + "   (BREAK: arming a breaching rollout for SigNoz)")
        with tracer.start_as_current_span("workload.break", kind=SpanKind.INTERNAL) as bs:
            bs.set_attribute("heal.scenario", scenario.name)
            _run_canary(controls, f"break-{cycle}", "heal.canary.pre")
        telemetry.shutdown()
        print("\nbreaching telemetry emitted; SigNoz will fire the alert on its next eval.")
        return

    pre = f"heal-{cycle}-pre"
    _banner(scenario.title + "   (SigNoz = sensor + scoreboard)")
    print(f"  policy: {policy.summary()}")

    after = None
    chosen = None
    escalated = None
    label = None
    fp_obj = None
    decider = None
    # If a SigNoz alert (via heal_bridge) woke us, adopt its trace context so the
    # alert handoff and the whole heal are ONE distributed trace in SigNoz.
    parent_ctx = None
    _tp = os.getenv("TRACEPARENT")
    if _tp:
        parent_ctx = TraceContextTextMapPropagator().extract({"traceparent": _tp})
    with tracer.start_as_current_span("agent.heal", kind=SpanKind.SERVER,
                                      context=parent_ctx) as root:
        trace_id_hex = format(root.get_span_context().trace_id, "032x")
        root.set_attribute("heal.cycle", cycle)
        root.set_attribute("heal.scenario", scenario.name)
        root.set_attribute("heal.policy.autonomy", policy.autonomy)
        root.set_attribute("service.managed", heal_sensors.TARGET_SERVICE)
        if args.triggered_by:
            root.set_attribute("heal.triggered", True)
            root.set_attribute("heal.trigger.alert", args.triggered_by)
            print(f"  triggered by SigNoz alert: {args.triggered_by}")

        # ---- BREAK IT: a rollout under the injected fault -----------------
        print(f"\n[SETUP]  managed workload is sick: {scenario.name} incident seeded.")
        _run_canary(controls, pre, "heal.canary.pre")

        # ---- DETECT -------------------------------------------------------
        print("\n[DETECT] asking SigNoz (via MCP) whether the rollout breached its SLO...")
        with tracer.start_as_current_span("heal.detect", kind=SpanKind.INTERNAL) as ds:
            slo = _settle_read(scenario.sensor, mcp, pre, scenario.min_calls)
            ds.set_attribute("slo.name", slo["slo"])
            ds.set_attribute("slo.status", slo["status"])
            ds.set_attribute("slo.breached", slo["breached"])
            if "sigma" in slo:
                ds.set_attribute("slo.sigma", slo["sigma"])
                ds.set_attribute("slo.baseline", slo["baseline"])
            if slo.get("fingerprint"):
                fp_obj = heal_fingerprint.Fingerprint(**slo["fingerprint"])
                for k, v in fp_obj.as_attrs().items():
                    ds.set_attribute(k, v)
            print("  " + slo["headline"])

        # A blind sensor must NEVER be read as 'healthy'. _settle_read already
        # exhausted its retries for a retryable (ingestion-lag) UNKNOWN, so ANY
        # UNKNOWN that survives to here means we still cannot see the workload --
        # e.g. a canary that produced no telemetry because Ollama was down. Refuse
        # to claim there is nothing to heal; surface it and stop.
        if slo["status"] == heal_sensors.STATUS_UNKNOWN:
            root.set_attribute("heal.sensor_blind", True)
            root.set_status(Status(StatusCode.ERROR, "sensor blind: " + slo.get("reason", "")))
            _banner("SENSOR BLIND -- REFUSING TO ACT")
            print(f"  {slo['headline']}")
            print("  the healer will not act while it cannot observe the workload.")
            telemetry.shutdown()
            return
        if slo["status"] != heal_sensors.STATUS_BREACH:
            print("  no breach detected -- nothing to heal. (Is the fault firing?)")
            root.set_status(Status(StatusCode.OK))
            telemetry.shutdown()
            return
        heal_metrics.breach(slo["slo"], pre)
        scenario.record(slo, pre, "pre")
        label, pre_str, _ = scenario.value_of(slo)
        root.set_attribute("heal.breach", pre_str)
        if slo.get("fingerprint"):
            root.set_attribute("heal.fingerprint.class", slo["fingerprint"]["class_id"])
            root.set_attribute("heal.fingerprint.severity", slo["fingerprint"]["severity"])
        _hlog("breach.detected", slo=slo["slo"], cohort=pre, value=pre_str,
              severity=(slo.get("fingerprint") or {}).get("severity", "n/a"),
              fingerprint=(slo.get("fingerprint") or {}).get("class_id", "n/a"),
              anomaly_only=bool(slo.get("anomaly_only")))

        # A statistical anomaly that is NOT also a fixed-floor breach is real but
        # lower-confidence: propose it (autonomy capped at 'suggest'), never
        # auto-apply. A hard-floor breach keeps the configured autonomy.
        episode_policy = policy
        if slo.get("anomaly_only"):
            episode_policy = heal_policy.Policy(autonomy="suggest")
            root.set_attribute("heal.anomaly_only", True)
            print("  (statistical anomaly, not a fixed-floor breach -> autonomy capped at 'suggest')")
        t_breach = time.time()

        healed = False
        attempt = 0
        actions_left = list(scenario.actions)
        while not healed and attempt < MAX_HEAL_ATTEMPTS and actions_left:
            attempt += 1
            snap = controls.snapshot()   # the point this action can be rolled back to

            # ---- RECALL or DIAGNOSE + DECIDE -----------------------------
            decisions, gate_log = [], []
            schemas, registry = heal_actuators.build(
                mcp, controls, pre, decisions, actions=tuple(actions_left),
                policy=episode_policy, gate_log=gate_log)

            # First, try to RECALL a verified fix for this exact incident class.
            # A hit replays the known-good action deterministically -- no LLM call.
            # (Skipped for statistical-only anomalies, which stay human-in-the-loop.)
            mem = None
            if attempt == 1 and fp_obj is not None and not slo.get("anomaly_only"):
                mem = heal_memory.recall(fp_obj, allowed=actions_left)

            if mem is not None:
                heal_metrics.recall("hit", fp_obj.class_id)
                decider = "memory"
                action = mem["action_base"]
                print(f"\n[RECALL] a SigNoz-verified fix for this incident class is on record "
                      f"(proven {mem.get('count', 0)}x, trace {mem.get('trace_id', '')[:12]}); "
                      f"replaying '{action}' with NO model call.")
                with tracer.start_as_current_span("heal.decide", kind=SpanKind.INTERNAL) as rspan:
                    rspan.set_attribute("heal.decider", "memory")
                    rspan.set_attribute("heal.recall.hit", True)
                    rspan.set_attribute("heal.recall.source_trace", mem.get("trace_id", ""))
                    rspan.set_attribute("heal.recall.proven_severity", mem.get("proven_severity", ""))
                    rspan.set_attribute("heal.recall.times_proven", mem.get("count", 0))
                    with tracer.start_as_current_span(f"tool.{action}", kind=SpanKind.INTERNAL) as ts:
                        ts.set_attribute("tool.name", action)
                        ts.set_attribute("heal.replayed", True)
                        res = registry[action]()
                if not res.get("applied"):
                    print(f"  recalled fix not applied ({res.get('policy') or res.get('reason')}); "
                          f"falling back to the model.")
                    mem = None
                else:
                    chosen = action
                    _hlog("decision.recall", action=action, source="memory",
                          times_proven=mem.get("count", 0),
                          source_trace=str(mem.get("trace_id", ""))[:16])

            if mem is None:
                heal_metrics.recall("miss", fp_obj.class_id if fp_obj else "unknown")
                # ---- DIAGNOSE + DECIDE (the agentic step, via MCP) -------
                # Tiered routing: the local model decides by default. If a prior
                # attempt's fix did not hold AND a cloud escalation tier is
                # configured, escalate THIS decision to the stronger hosted model.
                # With no cloud tier set (the default) this always stays local, so
                # the loop is fully offline.
                route = "escalation" if (config.ESCALATION_ENABLED and attempt > 1) else "local"
                eff_tier = config.tier(route)["name"]
                print(f"\n[DIAGNOSE] attempt {attempt}: {eff_tier} model {config.tier(route)['model']} "
                      f"reads the incident via MCP and decides on a fix...")
                healer = Agent(tool_schemas=schemas, registry=registry,
                               system_prompt=scenario.system, root_span="heal.decide",
                               temperature=0.0, tier=route)
                try:
                    healer.invoke(scenario.task.format(cohort=pre))
                except Exception as e:  # noqa: BLE001
                    print("  decide step raised:", e)

                for d in gate_log:
                    heal_metrics.policy("allowed" if d.allow else
                                        ("held" if d.requires_approval else "denied"), d.action)

                chosen = next((d for d in decisions if d != "read_incident"), None)
                decider = "llm"   # the model chose from the evidence (default path)
                root.set_attribute("heal.decider.tier", eff_tier)
                if not chosen:
                    held = [d for d in gate_log if not d.allow and d.requires_approval]
                    if held:
                        escalated = "awaiting_approval"
                        root.set_attribute("heal.escalated", escalated)
                        root.set_attribute("heal.decision.source", "human")
                        heal_metrics.decision("human", held[-1].action)
                        print(f"  [POLICY] remediation held for human approval "
                              f"({held[-1].action}: {held[-1].reason}); escalating.")
                        _hlog("escalated", level=logging.WARNING, reason="policy_hold",
                              action=held[-1].action, detail=held[-1].reason)
                        break
                    # Safety net: the model read the incident but didn't act. Apply
                    # the scenario's default fix ourselves -- still policy-gated.
                    chosen = scenario.fallback
                    decider = "fallback"
                    with tracer.start_as_current_span(f"tool.{chosen}", kind=SpanKind.INTERNAL) as fs:
                        fs.set_attribute("tool.name", chosen)
                        fs.set_attribute("heal.fallback", True)
                        res = registry[chosen]()
                    if not res.get("applied"):
                        escalated = "awaiting_approval"
                        root.set_attribute("heal.escalated", escalated)
                        root.set_attribute("heal.decision.source", "human")
                        heal_metrics.decision("human", chosen)
                        print(f"  [POLICY] default remediation {chosen} not applied "
                              f"({res.get('policy')}); escalating.")
                        _hlog("escalated", level=logging.WARNING, reason="default_blocked",
                              action=chosen, detail=str(res.get("policy")))
                        break
                    print("  (model did not act; applied safe default remediation)")

            chosen = _base_action(chosen)
            print(f"[ACT]     remediation applied: {chosen}  (source={decider})")
            _hlog("action.applied", action=chosen, source=decider, attempt=attempt,
                  tier=locals().get("eff_tier", "n/a"))
            heal_metrics.action(chosen)
            heal_metrics.decision(decider, chosen)
            root.set_attribute("heal.decision.source", decider)
            root.set_attribute("heal.recall.hit", decider == "memory")
            root.set_attribute(f"heal.action.{attempt}", chosen)
            actions_left = [a for a in actions_left
                            if a != chosen and not chosen.startswith(a)]

            # ---- VERIFY: a rollout under the fixed config ----------------
            post = f"heal-{cycle}-post{attempt}"
            print(f"\n[VERIFY]  rolling out '{post}' under the fixed config, then "
                  f"re-checking SigNoz...")
            _run_canary(controls, post, "heal.canary.post")
            with tracer.start_as_current_span("heal.verify", kind=SpanKind.INTERNAL) as vs:
                after = _settle_read(scenario.sensor, mcp, post, scenario.min_calls)
                vs.set_attribute("slo.status", after["status"])
                vs.set_attribute("slo.breached", after["breached"])
                print("  " + after["headline"])
            _hlog("verify", slo=slo["slo"], cohort=post, status=after["status"],
                  breached=bool(after["breached"]), healed=after["status"] == heal_sensors.STATUS_PASS)
            # Only a positive PASS counts as healed. BREACH or UNKNOWN (blind) both
            # mean 'not verified healed' -> roll the change back.
            healed = after["status"] == heal_sensors.STATUS_PASS
            if after.get("known"):
                scenario.record(after, post, "post")
            elif after["status"] == heal_sensors.STATUS_UNKNOWN:
                print("  could not verify the fix (sensor blind); treating as NOT healed.")

            if not healed:
                # ---- ROLLBACK: the action didn't clear the breach -> revert it.
                with tracer.start_as_current_span("heal.rollback", kind=SpanKind.INTERNAL) as rs:
                    rs.set_attribute("heal.rolled_back_action", chosen)
                    rs.set_attribute("heal.attempt", attempt)
                    controls.restore(snap)
                    print(f"  [ROLLBACK] '{chosen}' did not clear the breach; reverted the "
                          f"control-plane change to its pre-action snapshot.")
                _hlog("rollback", level=logging.WARNING, action=chosen, attempt=attempt,
                      status=after["status"])
                heal_metrics.rollback(chosen)
                root.set_attribute(f"heal.rollback.{attempt}", chosen)

        # ---- OUTCOME ------------------------------------------------------
        mttr_ms = (time.time() - t_breach) * 1000
        heal_metrics.result(slo["slo"], healed)
        heal_metrics.mttr(mttr_ms, slo["slo"])
        root.set_attribute("heal.healed", healed)
        root.set_attribute("heal.mttr_ms", round(mttr_ms))
        _hlog("outcome", level=(logging.INFO if healed else logging.WARNING),
              slo=slo["slo"], healed=healed, escalated=bool(escalated),
              action=chosen or "none", decider=decider or "none",
              mttr_s=round(mttr_ms / 1000, 1))

        # LEARN: a verified heal (SigNoz-confirmed) becomes episodic memory, so
        # the next occurrence of this incident class can be replayed with no LLM.
        if healed and chosen and fp_obj is not None and not slo.get("anomaly_only"):
            rec = heal_memory.record_success(fp_obj, chosen, mttr_ms, trace_id_hex)
            root.set_attribute("heal.memory.recorded", True)
            root.set_attribute("heal.memory.times_proven", rec.get("count", 0))
            print(f"  learned: '{chosen}' recorded as a verified fix for incident class "
                  f"{fp_obj.class_id} (proven {rec.get('count', 0)}x).")
        root.set_status(Status(StatusCode.OK))

        if escalated:
            _banner("NOT HEALED -- ESCALATED FOR HUMAN APPROVAL")
        else:
            _banner("HEALED" if healed else "NOT HEALED -- escalate")
        pre_str = scenario.value_of(slo)[1]
        if after is not None and after.get("known"):
            post_str = scenario.value_of(after)[1]
        elif after is not None:
            post_str = "(unverified -- sensor blind)"
        else:
            post_str = "(no remediation applied)"
        print(f"  {label}:  {pre_str}  ->  {post_str}")
        print(f"  remediation: {chosen if chosen else '(none -- held for approval)'}")
        print(f"  policy:      {policy.summary()}")
        print(f"  MTTR:        {mttr_ms / 1000:.0f}s   (breach detected -> verified)")
        print(f"  trace:       agent.heal   {trace_id_hex}")
        print(f"  view:        {SIGNOZ_UI}/trace/{trace_id_hex}")

        # ---- IMPACT: translate the heal into real money -------------------
        # Benchmarks (LLM list prices + cost of downtime) that a company plugs
        # in via economics.yaml. Estimates, clearly labeled, not an invoice.
        impact = economics.impact_report(
            spend_before=slo.get("spend_per_request_usd"),
            spend_after=(after.get("spend_per_request_usd") if after else None),
            mttr_s=mttr_ms / 1000)
        if impact["lines"]:
            print("  " + "-" * 64)
            print("  [IMPACT]  real-money view (benchmarks, tune in economics.yaml):")
            for ln in impact["lines"]:
                print(f"            {ln}")
            root.set_attribute("heal.impact.downtime_profile", impact["downtime_profile"])
            if "monthly_spend_saved_usd" in impact:
                root.set_attribute("heal.impact.monthly_spend_saved_usd",
                                   round(impact["monthly_spend_saved_usd"], 2))

    telemetry.shutdown()
    print("\nflushed self-healer traces + metrics to SigNoz.")


if __name__ == "__main__":
    main()
