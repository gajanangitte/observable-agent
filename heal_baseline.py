"""Persisted baseline of the healer's own recent HEALTHY observations.

The robust-stats detectors (``heal_stats``) judge a live reading against "what
normal looks like for this service". That baseline is the agent's own
SigNoz-derived sensor readings over time: every PASS reading is appended here, so
the distribution is the service's genuine healthy behaviour. Breaching readings
are never appended, so an incident can never poison the baseline it is judged
against.

Stored as a tiny JSON ring buffer keyed by SLO name, next to ``heal_state.json``.
It is runtime state (like ``heal_state.json``), not source, and starts empty on a
fresh clone -- in which case the fixed SLO floor alone guards (the statistical
supplement stays silent until it has >= 3 healthy samples).
"""
import json
import os
import threading

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heal_baseline.json")
MAX_SAMPLES = int(os.getenv("HEAL_BASELINE_MAX", "60"))

_lock = threading.Lock()


def _load():
    try:
        with open(PATH) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def series(slo):
    """The recent healthy history for an SLO (oldest first)."""
    return [float(x) for x in _load().get(slo, [])]


def append(slo, value):
    """Record one healthy observation; keep only the last MAX_SAMPLES."""
    with _lock:
        d = _load()
        xs = [float(x) for x in d.get(slo, [])]
        xs.append(round(float(value), 6))
        d[slo] = xs[-MAX_SAMPLES:]
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, PATH)
        return list(d[slo])
