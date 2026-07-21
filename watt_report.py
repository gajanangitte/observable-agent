"""Run the WattTrace GreenOps suite and publish the verdict to SigNoz.

WattTrace answers a question a token-cost dashboard cannot: how much energy did
we burn PER VERIFIED ANSWER, and how much of it was wasted on retries and failed
work? It drives the real local agent over a fixed, deterministically graded
question set, estimates the energy of every ``llm.chat`` call from active power
draw times active compute time (by default the time implied by the token counts,
see ``energy.py``), and emits the whole run to SigNoz
as all three OpenTelemetry signals on the ``watttrace`` service:

  * TRACES  -- one ``watt.suite`` root span, a ``watt.cohort`` span per cohort
    carrying the GreenOps verdict (``watttrace.status`` = PASS/BREACH/UNKNOWN),
    and, underneath, each ``agent.invoke`` with its energy-tagged ``llm.chat``
    child spans (a dropped retry is stamped ``watttrace.energy.disposition=wasted``).
  * METRICS -- ``watttrace.energy.consumed`` (J), ``watttrace.carbon.emitted`` (g),
    ``watttrace.answer.count`` (labelled verified true/false),
    ``watttrace.token.count``, ``watttrace.inference.duration``.
  * LOGS    -- the agent's normal trace-correlated logs, plus the printed
    scoreboard.

The north star is JOULES PER VERIFIED ANSWER. Because the verdict is fail closed
(UNKNOWN below a minimum sample, never a false all-clear), the suite doubles as a
CI GreenOps gate: with ``--gate`` it exits non-zero when a cohort breaches the
energy budget.

    python watt_report.py                       # control vs retry-fault, compared
    python watt_report.py --cohort control       # one clean cohort
    python watt_report.py --fault retry --gate    # fail CI if the fault breaches budget
    python watt_report.py --questions 3 --json watt_report.json
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("OTEL_SERVICE_NAME", "watttrace")
# Make the always-on energy hook in telemetry.record_llm fire for this run.
os.environ["WATTTRACE_LIVE"] = "1"

import config
import energy
import telemetry
import watt_metrics
from agent import Agent

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

HERE = Path(__file__).resolve().parent

# A small, deterministically graded golden set. Each answer is VERIFIED by a
# fail-closed keyword check against the agent's known-good facts (see tools.py):
# a real answer that used the tools contains the service's true status word. This
# is a deterministic verifier, never an LLM judge (whose own energy and
# nondeterminism would contaminate the score).
GOLDEN = [
    {"q": "What is the current status of the payments service? Answer in one sentence.",
     "must": ["payment", "healthy"],
     "must_not": ["not healthy", "unhealthy", "degraded", "down", "unavailable"]},
    {"q": "Is the search service healthy right now? Answer in one sentence.",
     "must": ["search", "healthy"],
     "must_not": ["not healthy", "unhealthy", "degraded", "down", "unavailable"]},
    {"q": "What is the status of the checkout service? Answer in one sentence.",
     "must": ["checkout", "degraded"],
     "must_not": ["not degraded", "is healthy", "stable"]},
    {"q": "What is the status of the inventory service? Answer in one sentence.",
     "must": ["inventory", "degraded"],
     "must_not": ["not degraded", "is healthy", "stable"]},
    {"q": "Which service is healthy: payments or checkout? Answer with the service name.",
     "must": ["payment"],
     "must_not": ["or checkout", "checkout is healthy", "both", "neither", "not sure"]},
]


def verify(answer: str, must, must_not=None) -> bool:
    """Fail-closed: an answer counts as verified only if it states every required
    fact (``must``) and none of the contradicting ones (``must_not``). Missing or
    empty answers are not verified (never healthy by default). ``must_not`` is the
    defence against a fluent WRONG answer that still contains the right keywords,
    e.g. a negation ("payments is not healthy") or a bare echo of the question."""
    a = (answer or "").lower()
    if not a:
        return False
    if not all(k.lower() in a for k in must):
        return False
    if must_not and any(k.lower() in a for k in must_not):
        return False
    return True


def run_cohort(agent, tracer, name, chaos, questions):
    """Drive one cohort, record its energy, and return (snapshot, verified, verdict)."""
    config.EXPERIMENT_ID = f"watt-{name}"
    config.CHAOS_DROP_ONCE = bool(chaos)
    watt_metrics.reset()
    verified = 0
    t0 = time.perf_counter()
    with tracer.start_as_current_span("watt.cohort", kind=SpanKind.INTERNAL) as cs:
        cs.set_attribute("watt.cohort", name)
        cs.set_attribute("experiment.id", config.EXPERIMENT_ID)
        cs.set_attribute("watt.fault", "retry_drop" if chaos else "none")
        for item in questions:
            with tracer.start_as_current_span("watt.answer", kind=SpanKind.INTERNAL) as ans_span:
                ans_span.set_attribute("watt.cohort", name)
                ans_span.set_attribute("agent.question", item["q"])
                try:
                    answer = agent.invoke(item["q"])
                except Exception as e:  # a crashed answer is simply not verified
                    answer = ""
                    ans_span.set_status(Status(StatusCode.ERROR, str(e)))
                ok = verify(answer, item["must"], item.get("must_not"))
                verified += 1 if ok else 0
                ans_span.set_attribute("watttrace.answer.verified", ok)
                watt_metrics.record_answer(agent._model, ok)

        snap = watt_metrics.snapshot()
        v = energy.verdict(snap["joules"], snap["grams"], verified)
        cs.set_attribute("watttrace.status", v.status)
        cs.set_attribute("watttrace.verified_answers", verified)
        cs.set_attribute("watttrace.answers_total", len(questions))
        cs.set_attribute("watttrace.energy_joules", round(snap["joules"], 2))
        cs.set_attribute("watttrace.carbon_grams", round(snap["grams"], 5))
        cs.set_attribute("watttrace.wasted_joules", round(snap["wasted_joules"], 2))
        cs.set_attribute("watttrace.estimate.quality", snap["quality"])
        if v.joules_per_verified_answer is not None:
            cs.set_attribute("watttrace.joules_per_verified_answer",
                             round(v.joules_per_verified_answer, 2))
        # BREACH is an operational error the alert keys on.
        if v.status == energy.BREACH:
            cs.set_status(Status(StatusCode.ERROR, v.reason))
        else:
            cs.set_status(Status(StatusCode.OK))
    snap["wall_s"] = time.perf_counter() - t0
    return snap, verified, v


def scoreboard(rows, regression):
    print("\n" + "=" * 72)
    print("  WATTTRACE GREENOPS SCOREBOARD  (joules per verified answer)")
    print("=" * 72)
    en = energy._get()
    print(f"  power model:    {en.tier().active_watts:.0f} W active "
          f"({en.tier().name}, {en.tier().quality}), PUE {en.pue:g}, "
          f"grid {en.region().name} {en.region().gco2_per_kwh:.0f} gCO2e/kWh")
    print("-" * 72)
    for r in rows:
        v = r["verdict"]
        jpa = f"{v.joules_per_verified_answer:.0f} J" if v.joules_per_verified_answer is not None else "UNKNOWN"
        print(f"  [{r['name']:^8}] verdict {v.status:<7} "
              f"energy/verified answer: {jpa:<10} "
              f"({r['verified']}/{r['total']} verified)")
        print(f"             {r['snap']['calls']} calls, "
              f"{r['snap']['joules']:.0f} J total ({en.wh(r['snap']['joules']):.2f} Wh), "
              f"{r['snap']['grams']:.3f} g CO2e, "
              f"wasted {r['snap']['wasted_joules']:.0f} J on {r['snap']['wasted_calls']} retries")
    if regression is not None:
        print("-" * 72)
        print(f"  REGRESSION: the retry fault raised energy per verified answer by "
              f"{regression:.0f} percent")
        print(f"              for the SAME verified answers. That extra energy is pure waste.")
    print("=" * 72)


def run(cohorts, questions, gate, export, json_out):
    if export:
        telemetry.setup_telemetry()
    agent = Agent()
    tracer = trace.get_tracer("watttrace")

    qset = GOLDEN[:questions] if questions else GOLDEN
    # Warm up the model once (not measured) so the first real answer is not paying
    # a one-time model-load cost. Energy is token-based so this does not change any
    # figure; it just keeps the run brisk and the measured-time cross-check clean.
    try:
        config.CHAOS_DROP_ONCE = False
        agent.invoke("warmup: reply with ok")
    except Exception:
        pass
    plan = []
    if cohorts in ("both", "control"):
        plan.append(("control", False))
    if cohorts in ("both", "chaos", "fault"):
        plan.append(("chaos", True))

    rows = []
    trace_hex = ""
    with tracer.start_as_current_span("watt.suite", kind=SpanKind.SERVER) as suite:
        suite.set_attribute("watt.questions", len(qset))
        suite.set_attribute("service.name", "watttrace")
        ctx = suite.get_span_context()
        trace_hex = format(ctx.trace_id, "032x") if ctx and ctx.trace_id else ""
        worst = energy.PASS
        for name, chaos in plan:
            print(f"\n>>> cohort '{name}' ({'retry fault' if chaos else 'clean'}): "
                  f"{len(qset)} questions ...")
            snap, verified, v = run_cohort(agent, tracer, name, chaos, qset)
            rows.append({"name": name, "snap": snap, "verified": verified,
                         "total": len(qset), "verdict": v})
            if v.status == energy.BREACH:
                worst = energy.BREACH
            elif v.status == energy.UNKNOWN and worst != energy.BREACH:
                worst = energy.UNKNOWN
        suite.set_attribute("watttrace.worst_status", worst)

    # Regression: chaos vs control energy per verified answer (same answers).
    regression = None
    by = {r["name"]: r for r in rows}
    if "control" in by and "chaos" in by:
        c = by["control"]["verdict"].joules_per_verified_answer
        k = by["chaos"]["verdict"].joules_per_verified_answer
        if c and k and c > 0:
            regression = 100.0 * (k - c) / c

    scoreboard(rows, regression)

    # The impact report reads off the worst cohort actually run.
    focus = by.get("chaos") or by.get("control") or rows[0]
    for line in energy.impact_report(verdict=focus["verdict"],
                                     wasted_joules=focus["snap"]["wasted_joules"],
                                     quality=focus["snap"]["quality"])["lines"]:
        print("  " + line)

    report = {
        "service": "watttrace",
        "trace": trace_hex,
        "power_watts": energy._get().tier().active_watts,
        "power_tier": energy._get().tier().name,
        "estimate_quality": energy._get().tier().quality,
        "region": energy._get().region().name,
        "budget_joules_per_verified_answer": energy.budget_joules_per_verified_answer(),
        "regression_percent": regression,
        "cohorts": [
            {"name": r["name"], "status": r["verdict"].status,
             "joules_per_verified_answer": r["verdict"].joules_per_verified_answer,
             "gco2_per_verified_answer": r["verdict"].gco2_per_verified_answer,
             "verified": r["verified"], "total": r["total"],
             "joules": r["snap"]["joules"], "grams": r["snap"]["grams"],
             "wasted_joules": r["snap"]["wasted_joules"], "calls": r["snap"]["calls"]}
            for r in rows
        ],
    }
    out = json_out or (HERE / "watt_report.json")
    Path(out).write_text(json.dumps(report, indent=2))
    print(f"\nreport -> {out}")

    if export:
        telemetry.shutdown()
        if trace_hex:
            ui = os.getenv("SIGNOZ_UI", "http://localhost:8080")
            print(f"  watt suite trace: {trace_hex}")
            print(f"  view:             {ui}/trace/{trace_hex}")

    statuses = [r["verdict"].status for r in rows]
    if gate:
        if energy.BREACH in statuses:
            print("  GATE: a cohort BREACHED the GreenOps budget -> failing the build")
            return 1
        if energy.UNKNOWN in statuses:
            print("  GATE: a cohort is UNKNOWN (too few verified answers or no energy "
                  "recorded) -> failing closed, not green-lighting an unjudgeable run")
            return 2
    return 0


def main():
    ap = argparse.ArgumentParser(description="WattTrace GreenOps suite (SigNoz-native)")
    ap.add_argument("--cohort", choices=["both", "control", "chaos", "fault"],
                    default="both", help="which cohorts to run (default both)")
    ap.add_argument("--fault", choices=["retry", "none"], default=None,
                    help="alias: --fault retry runs the chaos cohort, --fault none the control")
    ap.add_argument("--questions", type=int, default=0,
                    help="cap the golden set to the first N questions (default all)")
    ap.add_argument("--gate", action="store_true",
                    help="exit non-zero if a cohort breaches the energy budget (CI GreenOps gate)")
    ap.add_argument("--no-export", action="store_true",
                    help="skip SigNoz export (offline scoreboard only)")
    ap.add_argument("--json", dest="json_out", default=None, help="write the JSON report here")
    args = ap.parse_args()
    cohorts = args.cohort
    if args.fault == "retry":
        cohorts = "chaos"
    elif args.fault == "none":
        cohorts = "control"
    code = run(cohorts, args.questions, args.gate, not args.no_export, args.json_out)
    sys.exit(code)


if __name__ == "__main__":
    main()
