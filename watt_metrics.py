"""WattTrace OpenTelemetry layer: turn each inference call into energy signals.

This is the I/O half of WattTrace. The deterministic physics lives in
``energy.py``; this module attaches those numbers to real OTel signals and keeps
a tiny in-process accumulator the runner reads to build its scoreboard.

It is deliberately decoupled: it does NOT import ``telemetry`` or ``agent`` (so
``telemetry`` can call ``on_llm`` without a cycle), it gets its meter straight
from the OTel API, and every public call is a safe no-op if telemetry was never
set up. The always-on live hook in ``telemetry.record_llm`` only fires when the
``WATTTRACE_LIVE`` environment variable is set, so the base agent is untouched
unless a GreenOps run explicitly asks for it.

Signals emitted (names shared with watt_dashboard.py and watt_alert.py):
  metrics: watttrace.energy.consumed (J), watttrace.carbon.emitted (g),
           watttrace.answer.count ({answer}), watttrace.token.count ({token}),
           watttrace.inference.duration (s)
  span attrs on the active llm.chat span: watttrace.energy.j, watttrace.carbon.g,
           watttrace.power.w, watttrace.pue, watttrace.grid.carbon_g_per_kwh,
           watttrace.energy.disposition (useful|wasted), watttrace.estimate.method,
           watttrace.estimate.quality, watttrace.estimate.scope
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from opentelemetry import metrics, trace

import energy

# --- lazy instruments --------------------------------------------------------
_INSTR: dict = {}


def _instruments() -> dict:
    """Create the WattTrace metric instruments once, on first use."""
    if _INSTR:
        return _INSTR
    meter = metrics.get_meter("watttrace")
    _INSTR["energy"] = meter.create_counter(
        "watttrace.energy.consumed", unit="J",
        description="Estimated electrical energy consumed by inference (power model)")
    _INSTR["carbon"] = meter.create_counter(
        "watttrace.carbon.emitted", unit="g",
        description="Estimated CO2e from inference energy on the configured grid")
    _INSTR["answers"] = meter.create_counter(
        "watttrace.answer.count", unit="{answer}",
        description="Answers produced, labelled by whether they passed verification")
    _INSTR["tokens"] = meter.create_counter(
        "watttrace.token.count", unit="{token}",
        description="Tokens processed (feeds joules per token)")
    _INSTR["duration"] = meter.create_histogram(
        "watttrace.inference.duration", unit="s",
        description="Wall-clock duration of one inference call")
    return _INSTR


# --- in-process accumulator (the runner's scoreboard source) -----------------
# One dict per recorded call. Kept small and cohort-scoped; reset() clears it.
_ACC: List[dict] = []


def reset() -> None:
    _ACC.clear()


def snapshot() -> dict:
    """Aggregate everything recorded since the last reset()."""
    joules = sum(r["joules"] for r in _ACC)
    grams = sum(r["grams"] for r in _ACC)
    wasted_j = sum(r["joules"] for r in _ACC if r["disposition"] == "wasted")
    wasted_g = sum(r["grams"] for r in _ACC if r["disposition"] == "wasted")
    tokens = sum(r["tokens"] for r in _ACC)
    calls = len(_ACC)
    wasted_calls = sum(1 for r in _ACC if r["disposition"] == "wasted")
    per_model: Dict[str, dict] = {}
    for r in _ACC:
        m = per_model.setdefault(r["model"], {"joules": 0.0, "grams": 0.0, "calls": 0})
        m["joules"] += r["joules"]
        m["grams"] += r["grams"]
        m["calls"] += 1
    quality = "MEASURED"
    order = {"MEASURED": 0, "ESTIMATED": 1, "FALLBACK": 2}
    for r in _ACC:  # the run is only as trustworthy as its weakest estimate
        if order.get(r["quality"], 1) > order.get(quality, 0):
            quality = r["quality"]
    return {
        "joules": joules, "grams": grams, "tokens": tokens, "calls": calls,
        "wasted_joules": wasted_j, "wasted_grams": wasted_g,
        "wasted_calls": wasted_calls, "per_model": per_model,
        "quality": quality if _ACC else "ESTIMATED",
    }


def _experiment_id() -> str:
    try:
        import config
        return getattr(config, "EXPERIMENT_ID", "") or ""
    except Exception:
        return os.getenv("EXPERIMENT_ID", "") or ""


def on_llm(model: str, input_tokens: int, output_tokens: int, latency_ms: float,
           status: str = "ok", tier: Optional[str] = None,
           region: Optional[str] = None) -> Optional[energy.CallEstimate]:
    """Record the energy footprint of one inference call.

    ``status`` mirrors ``telemetry.record_llm``: a "dropped" (retry tax) or
    "error" call burned real energy for no verified answer, so it is stamped
    ``disposition=wasted``. Returns the estimate, or None if it could not run.
    """
    try:
        tokens = int(input_tokens or 0) + int(output_tokens or 0)
        est = energy.estimate(
            input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0),
            wall_seconds=max(0.0, float(latency_ms)) / 1000.0,
            tier=tier, model=model, region=region)
        disposition = "wasted" if status in ("dropped", "error") else "useful"
        exp = _experiment_id()
        attrs = {"gen_ai.request.model": model, "status": status,
                 "watttrace.energy.disposition": disposition,
                 "watttrace.estimate.quality": est.quality}
        if exp:
            attrs["experiment.id"] = exp
        instr = _instruments()
        instr["energy"].add(est.joules, attrs)
        instr["carbon"].add(est.grams_co2, attrs)
        instr["duration"].record(est.measured_seconds or est.modelled_seconds, attrs)
        if input_tokens:
            instr["tokens"].add(int(input_tokens), {**attrs, "gen_ai.token.type": "input"})
        if output_tokens:
            instr["tokens"].add(int(output_tokens), {**attrs, "gen_ai.token.type": "output"})

        span = trace.get_current_span()
        if span and span.is_recording():
            span.set_attribute("watttrace.energy.j", round(est.joules, 3))
            span.set_attribute("watttrace.carbon.g", round(est.grams_co2, 6))
            span.set_attribute("watttrace.power.w", round(est.watts, 2))
            span.set_attribute("watttrace.pue", energy._get().pue)
            span.set_attribute("watttrace.grid.carbon_g_per_kwh",
                               energy._get().region(region).gco2_per_kwh)
            span.set_attribute("watttrace.energy.disposition", disposition)
            span.set_attribute("watttrace.energy.basis", est.basis)
            span.set_attribute("watttrace.time.modelled_s", round(est.modelled_seconds, 3))
            if est.measured_seconds is not None:
                span.set_attribute("watttrace.time.measured_s", round(est.measured_seconds, 3))
            span.set_attribute("watttrace.estimate.method", est.method)
            span.set_attribute("watttrace.estimate.quality", est.quality)
            span.set_attribute("watttrace.estimate.scope", est.scope)
            if est.joules_per_token is not None:
                span.set_attribute("watttrace.energy.j_per_token",
                                   round(est.joules_per_token, 4))

        _ACC.append({"model": model, "joules": est.joules, "grams": est.grams_co2,
                     "tokens": tokens, "disposition": disposition,
                     "quality": est.quality, "status": status})
        return est
    except Exception:  # never let energy accounting break a real inference path
        return None


def record_answer(model: str, verified: bool) -> None:
    """Count one produced answer, labelled by verification (the denominator).

    Both verified and unverified answers are counted so the dashboard can show a
    verified-answer RATE, not just a count.
    """
    try:
        exp = _experiment_id()
        attrs = {"gen_ai.request.model": model,
                 "verified": "true" if verified else "false"}
        if exp:
            attrs["experiment.id"] = exp
        _instruments()["answers"].add(1, attrs)
    except Exception:
        pass
