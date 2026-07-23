"""AccessTrace OpenTelemetry metrics: turn each WCAG journey into signals.

The pure grading lives in ``access_audit.py``; this module attaches those numbers
to real OTel metric instruments on the ``accesstrace`` meter. It is deliberately
decoupled: it does NOT import ``telemetry`` or ``agent``, it gets its meter straight
from the OTel API, and every public call is a safe no-op if telemetry was never set
up, so importing it offline (for tests) never touches the network.

Signals emitted (names shared with access_dashboard.py and access_alert.py):
  accesstrace.violation.count ({violation}, labelled impact + cohort)
  accesstrace.node.count     ({element}, offending elements, labelled impact + cohort)
  accesstrace.journey.count  ({journey}, labelled status + cohort)
  accesstrace.weighted_score ({score} histogram, per journey)
  accesstrace.focusable.count ({element}, keyboard-reachable elements per step)
"""
from __future__ import annotations

from opentelemetry import metrics

import access_audit as audit

_INSTR: dict = {}


def _instruments() -> dict:
    if _INSTR:
        return _INSTR
    meter = metrics.get_meter("accesstrace")
    _INSTR["violations"] = meter.create_counter(
        "accesstrace.violation.count", unit="{violation}",
        description="WCAG rule violations found, labelled by impact severity and cohort")
    _INSTR["nodes"] = meter.create_counter(
        "accesstrace.node.count", unit="{element}",
        description="Offending elements (nodes) behind the violations, by impact and cohort")
    _INSTR["journeys"] = meter.create_counter(
        "accesstrace.journey.count", unit="{journey}",
        description="Accessibility journeys graded, labelled by three-state status and cohort")
    _INSTR["weighted"] = meter.create_histogram(
        "accesstrace.weighted_score", unit="{score}",
        description="Severity-weighted violation score per journey (lower is better)")
    _INSTR["focusables"] = meter.create_counter(
        "accesstrace.focusable.count", unit="{element}",
        description="Keyboard-reachable elements exercised per journey step")
    return _INSTR


def record_journey(cohort: str, verdict) -> None:
    """Emit the counts and weighted score for one graded journey verdict."""
    try:
        instr = _instruments()
        base = {"cohort": cohort}
        for imp in audit.IMPACTS:
            n = verdict.counts.get(imp, 0)
            if n:
                instr["violations"].add(n, {**base, "impact": imp})
            nodes = verdict.node_counts.get(imp, 0)
            if nodes:
                instr["nodes"].add(nodes, {**base, "impact": imp})
        instr["journeys"].add(1, {**base, "status": verdict.status})
        instr["weighted"].record(verdict.weighted_score, base)
    except Exception:  # never let metrics break a run
        pass


def record_focusables(cohort: str, step: str, count: int) -> None:
    try:
        if count:
            _instruments()["focusables"].add(int(count), {"cohort": cohort, "step": step})
    except Exception:
        pass
