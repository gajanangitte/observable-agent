"""Verified remediation memory: SigNoz-confirmed heals become episodic memory.

The healer's differentiator. The FIRST time an incident class is seen, the local
model reads the evidence and chooses a fix; only after SigNoz *verifies* the fix
worked is that (fingerprint -> action) pair written here as a verified record. The
NEXT time the same incident class appears, the healer recalls the known-good fix
and replays it deterministically -- no LLM call, so it is faster, cheaper, and
carries no model-nondeterminism risk -- still through the policy gate and still
verified against SigNoz afterwards.

Two safety properties make this safe to run in production (a deterministic
lookup-and-replay of proven fixes, NOT an exploring bandit):

  1. Only VERIFIED heals are ever stored (``record_success`` is called after the
     SigNoz re-check passes), so memory never suggests something unproven.
  2. Recall is gated on severity: a fix is only replayed when the live incident
     is no WORSE than the severity the fix was proven against. A much more severe
     recurrence falls back to the model, which then re-proves (and re-records) it.

The store is a small JSON file (runtime state, like ``heal_state.json``); it
starts empty on a fresh clone, so the agent visibly *learns* over its first runs.
"""
import json
import os
import time

import heal_fingerprint

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heal_memory.json")


def _load():
    try:
        with open(PATH) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save(d):
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, PATH)


def _base(action):
    """The registry key for a chosen action (strip any ':arg' suffix)."""
    return (action or "").split(":", 1)[0]


def recall(fp, allowed):
    """Return the best verified record for this incident class, or None.

    ``fp`` is a ``heal_fingerprint.Fingerprint``; ``allowed`` is the set of action
    base-names currently offered for the incident. Among verified records that
    match the class, whose action is still allowed, and whose proven severity is
    at least the live severity, the most-proven (highest ``count``, then most
    recent) record wins.
    """
    allowed = set(allowed or [])
    live_rank = heal_fingerprint.severity_rank(fp.severity)
    hits = [
        r for r in _load().values()
        if r.get("verified")
        and r.get("class_id") == fp.class_id
        and r.get("action_base") in allowed
        and heal_fingerprint.severity_rank(r.get("proven_severity", "none")) >= live_rank
    ]
    if not hits:
        return None
    hits.sort(key=lambda r: (r.get("count", 0), r.get("last_used", 0)), reverse=True)
    return hits[0]


def record_success(fp, action, mttr_ms, trace_id):
    """Persist (or reinforce) a VERIFIED fix for an incident class.

    Call this only after SigNoz has confirmed the fix cleared the breach. Keying
    by (class_id, action) lets more than one proven fix coexist for a class; the
    proven severity is ratcheted up to the worst case the fix has handled.
    """
    base = _base(action)
    key = f"{fp.class_id}:{base}"
    now = time.time()
    d = _load()
    rec = d.get(key)
    if rec:
        rec["count"] = rec.get("count", 0) + 1
        rec["last_used"] = now
        rec["mttr_ms_last"] = round(mttr_ms)
        rec["mttr_ms_best"] = min(rec.get("mttr_ms_best", mttr_ms), round(mttr_ms))
        rec["trace_id"] = trace_id or rec.get("trace_id", "")
        if heal_fingerprint.severity_rank(fp.severity) > heal_fingerprint.severity_rank(
                rec.get("proven_severity", "none")):
            rec["proven_severity"] = fp.severity
    else:
        rec = {
            "class_id": fp.class_id, "slo": fp.slo,
            "fault_signature": fp.fault_signature,
            "action_base": base, "action_full": action,
            "proven_severity": fp.severity,
            "count": 1, "verified": True,
            "mttr_ms_last": round(mttr_ms), "mttr_ms_best": round(mttr_ms),
            "trace_id": trace_id or "", "first_seen": now, "last_used": now,
        }
    d[key] = rec
    _save(d)
    return rec


def all_records():
    return list(_load().values())
