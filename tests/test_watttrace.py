"""Unit tests for the WattTrace runner and OTel accumulator (no network).

These cover the two pieces of WattTrace that are pure logic: the fail-closed answer
verifier the GreenOps scoreboard grades on (an answer counts only if it states every
required fact, and a missing answer is never healthy by default), and the in-process
accumulator in watt_metrics that turns each recorded inference into the run's
scoreboard, including the wasted-energy disposition and the weakest-estimate quality
fold. watt_metrics.on_llm runs fully offline: it estimates from energy.py, updates the
accumulator, and no-ops the OTel emit if telemetry was never set up.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import energy
import watt_metrics
# Importing the runner pulls in the agent; it must never require the network at
# import time. Clear the live-hook flag it sets so it cannot leak into other tests.
import watt_report
os.environ.pop("WATTTRACE_LIVE", None)

_WATT_ENV = (
    "WATT_BASIS", "WATT_PUE", "WATT_PREFILL_TPS", "WATT_DECODE_TPS",
    "WATT_ACTIVE_WATTS", "WATT_DEFAULT_TIER", "WATT_DEFAULT_REGION", "WATTTRACE_CONFIG",
)


def _fresh():
    """Default energy model + an empty accumulator."""
    for k in _WATT_ENV:
        os.environ.pop(k, None)
    energy.reload()
    watt_metrics.reset()


# --- the fail-closed verifier ------------------------------------------------
def test_verify_requires_every_keyword_case_insensitive():
    assert watt_report.verify("The payments service is healthy.", ["payment", "healthy"])
    # Different casing still verifies.
    assert watt_report.verify("PAYMENTS are HEALTHY", ["payment", "healthy"])
    # Missing one required fact fails.
    assert not watt_report.verify("payments are degraded", ["payment", "healthy"])


def test_verify_fails_closed_on_empty_or_missing():
    assert not watt_report.verify("", ["payment"])
    assert not watt_report.verify(None, ["payment"])
    # An answer that names nothing required is not verified.
    assert not watt_report.verify("I am not sure", ["checkout", "degraded"])


# --- the accumulator / snapshot ----------------------------------------------
def test_snapshot_aggregates_joules_tokens_and_calls():
    _fresh()
    # 100 in + 10 out at defaults: 100/200 + 10/8 = 1.75 s, 65 W -> 113.75 J.
    est = watt_metrics.on_llm("llama3.2:3b", 100, 10, 500.0, "ok")
    assert est is not None and abs(est.joules - 113.75) < 1e-6
    watt_metrics.on_llm("llama3.2:3b", 100, 10, 500.0, "ok")
    snap = watt_metrics.snapshot()
    assert snap["calls"] == 2
    assert snap["tokens"] == 220
    assert abs(snap["joules"] - 2 * 113.75) < 1e-6
    assert snap["wasted_calls"] == 0 and snap["wasted_joules"] == 0.0


def test_dropped_and_error_calls_are_wasted():
    _fresh()
    watt_metrics.on_llm("m", 100, 10, 100.0, "ok")       # useful
    drop = watt_metrics.on_llm("m", 100, 10, 100.0, "dropped")  # retry tax
    err = watt_metrics.on_llm("m", 100, 10, 100.0, "error")     # failed work
    snap = watt_metrics.snapshot()
    assert snap["calls"] == 3
    assert snap["wasted_calls"] == 2
    # Wasted joules are exactly the dropped + errored calls' energy.
    assert abs(snap["wasted_joules"] - (drop.joules + err.joules)) < 1e-6


def test_quality_folds_to_weakest_estimate():
    _fresh()
    # A run of clean hardware-proxy estimates reports ESTIMATED.
    watt_metrics.on_llm("m", 100, 10, 100.0, "ok")
    assert watt_metrics.snapshot()["quality"] == "ESTIMATED"
    # One fallback estimate drags the whole run's trustworthiness down to FALLBACK.
    watt_metrics.on_llm("m", 100, 10, 100.0, "ok", tier="generic-cpu-fallback")
    assert watt_metrics.snapshot()["quality"] == "FALLBACK"


def test_snapshot_splits_by_model():
    _fresh()
    watt_metrics.on_llm("model-a", 100, 10, 100.0, "ok")
    watt_metrics.on_llm("model-b", 100, 10, 100.0, "ok")
    watt_metrics.on_llm("model-b", 100, 10, 100.0, "ok")
    per = watt_metrics.snapshot()["per_model"]
    assert set(per) == {"model-a", "model-b"}
    assert per["model-a"]["calls"] == 1 and per["model-b"]["calls"] == 2


def test_reset_clears_the_accumulator():
    _fresh()
    watt_metrics.on_llm("m", 100, 10, 100.0, "ok")
    assert watt_metrics.snapshot()["calls"] == 1
    watt_metrics.reset()
    empty = watt_metrics.snapshot()
    assert empty["calls"] == 0 and empty["joules"] == 0.0
    # An empty run reports ESTIMATED, never a false MEASURED.
    assert empty["quality"] == "ESTIMATED"


def test_on_llm_never_raises_and_record_answer_is_safe():
    _fresh()
    # Odd inputs must not blow up a real inference path; on_llm swallows and no-ops.
    assert watt_metrics.on_llm("m", 0, 0, 0.0, "ok") is not None
    # record_answer is a pure OTel emit; offline it is a harmless no-op.
    watt_metrics.record_answer("m", True)
    watt_metrics.record_answer("m", False)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
