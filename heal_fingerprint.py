"""Deterministic incident fingerprint: the stable identity of an incident class.

A fingerprint is a PURE function of the detected breach evidence (never the
model's opinion), so the same kind of incident always maps to the same id. It is
the key the healer uses to *recall* a previously verified remediation
(``heal_memory``): match the fingerprint, replay the known-good fix with no LLM
call, then verify. Because it is deterministic it is trivially unit-tested and
carries zero risk of the "same string, different id" drift a hash of a formatted
message would have.

Two ids are produced:

  * ``class_id`` -- stable across severity: identifies the incident CLASS
                    (which SLO, which direction, which fault signature). This is
                    the recall key: a mild and a severe retry-tax are the same
                    class and can share a verified fix.
  * ``full_id``  -- includes the severity bucket, for precise dashboards/metrics
                    where "a 3x breach" and "a 30x breach" should be told apart.

Recall is still gated on more than the class match (see ``heal_memory``): the
stored fix must have VERIFIED healed, and the live severity must not be wildly
worse than the severity the fix was proven against.
"""
import hashlib
from dataclasses import dataclass

# Each SLO's canonical fault signature -- the mechanism, not a remediation.
_SLO_SIGNATURE = {
    "retry_tax": "retry.reason=response_dropped",
    "cost_runaway": "runaway_llm_loop",
    "latency": "latency_p95_breach",
}

# Severity buckets by how far the observed value is past the threshold.
_SEVERITY = ("none", "low", "med", "high")


def _observed_threshold(slo):
    """Pull (observed, threshold) out of a sensor result dict, per SLO type."""
    name = slo.get("slo")
    if name == "retry_tax":
        return float(slo.get("retry_rate", 0.0) or 0.0), float(slo.get("threshold", 0.05) or 0.05)
    if name == "cost_runaway":
        return (float(slo.get("calls_per_request", 0.0) or 0.0),
                float(slo.get("threshold_calls", 6) or 6))
    if name == "latency":
        return (float(slo.get("p95_ms") or 0.0),
                float(slo.get("threshold_ms", 60000) or 60000))
    return float(slo.get("observed", 0.0) or 0.0), float(slo.get("threshold", 1.0) or 1.0)


def severity_bucket(observed, threshold):
    """How bad is the breach, as a coarse ratio bucket (stable to small noise)."""
    if threshold <= 0:
        return "unknown"
    ratio = observed / threshold
    if ratio < 1.0:
        return "none"
    if ratio < 2.0:
        return "low"
    if ratio < 4.0:
        return "med"
    return "high"


def severity_rank(bucket):
    """Ordinal for a severity bucket, so recall can compare 'how much worse'."""
    try:
        return _SEVERITY.index(bucket)
    except ValueError:
        return -1


def _hash(*parts):
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Fingerprint:
    slo: str
    direction: str          # "over" == observed at/above threshold
    fault_signature: str
    severity: str           # none | low | med | high
    observed: float
    threshold: float
    class_id: str           # stable across severity -- the recall key
    full_id: str            # includes severity

    def as_attrs(self):
        """Span/metric attributes for an auditable fingerprint on the trace."""
        return {
            "heal.fingerprint.class": self.class_id,
            "heal.fingerprint.id": self.full_id,
            "heal.fingerprint.slo": self.slo,
            "heal.fingerprint.direction": self.direction,
            "heal.fingerprint.severity": self.severity,
            "heal.fingerprint.signature": self.fault_signature,
        }

    def as_dict(self):
        return {
            "slo": self.slo, "direction": self.direction,
            "fault_signature": self.fault_signature, "severity": self.severity,
            "observed": self.observed, "threshold": self.threshold,
            "class_id": self.class_id, "full_id": self.full_id,
        }


def fingerprint(slo):
    """Compute the deterministic fingerprint of a sensor result dict."""
    name = slo.get("slo", "unknown")
    observed, threshold = _observed_threshold(slo)
    direction = "over" if observed >= threshold else "under"
    signature = _SLO_SIGNATURE.get(name, name)
    severity = severity_bucket(observed, threshold)
    class_id = _hash(name, direction, signature)
    full_id = _hash(name, direction, signature, severity)
    return Fingerprint(name, direction, signature, severity, observed, threshold,
                       class_id, full_id)
