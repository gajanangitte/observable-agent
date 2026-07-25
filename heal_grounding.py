"""Groundedness auditor: an independent evidence gate on the healer's decision.

Detecting and acting are covered by the sensors and the policy gate. This module
answers a different, and increasingly important, question about an agentic loop:
**was the decision actually grounded in the evidence, or did the model just assert
a fix?** It is a pure, deterministic auditor that runs AFTER a remediation is
chosen and scores how well that decision is supported by the real SigNoz incident
evidence, producing a confidence tier (``HIGH`` / ``MEDIUM`` / ``LOW`` / ``NONE``)
that is stamped on the trace next to the action.

It checks four independent supports, none of which is the model's own opinion:

  1. **Evidence was consulted.** Did the decision path actually read the incident
     evidence out of SigNoz (``read_incident``), or was a fix applied without ever
     looking? A verified-memory replay counts as grounded by construction: it is a
     fix SigNoz already confirmed against real evidence.
  2. **The incident is a known class.** Does the breach map to a deterministic
     fingerprint class (a real, recognised fault), rather than an unrecognised or
     absent signal?
  3. **The action addresses the fault.** Is the chosen remediation actually one
     that targets THIS SLO's fault signature? An action that does not (however it
     was reached) is ungrounded, and is capped at ``NONE`` no matter what else is
     true -- the single hard gate here.
  4. **The breach is a fixed-floor breach**, not a statistical anomaly only. An
     anomaly-only signal is real but lower confidence, so it is capped at
     ``MEDIUM``.

The auditor never blocks (the policy gate is the safety authority); it makes the
QUALITY of each decision observable, so a low-confidence heal is visible in SigNoz
instead of looking identical to a high-confidence one. It is deliberately pure --
no telemetry, no network, no I/O -- so it is trivially unit-tested and carries no
risk of drift.
"""
from dataclasses import dataclass

# Which remediations actually address each SLO's fault mechanism. A decision to
# apply an action that is NOT in its SLO's set is not a grounded response to this
# fault -- it might "work" by luck, but the evidence does not support it.
_REMEDIATION_FOR_SLO = {
    "retry_tax": {"disable_fault_injection", "enable_mitigation"},
    "carbon_slo": {"disable_fault_injection", "enable_mitigation"},
    "cost_runaway": {"set_cost_budget", "switch_model"},
    "latency": {"switch_model"},
}

TIER_HIGH = "HIGH"
TIER_MEDIUM = "MEDIUM"
TIER_LOW = "LOW"
TIER_NONE = "NONE"

_TIER_RANK = {TIER_NONE: 0, TIER_LOW: 1, TIER_MEDIUM: 2, TIER_HIGH: 3}

# The four supports and how much each contributes to the raw support score. They
# sum to 1.0 when every support is present (a fully grounded, verified decision).
_W_EVIDENCE = 0.40
_W_ACTION = 0.30
_W_FINGERPRINT = 0.20
_W_HARDFLOOR = 0.10


def _bucket(score):
    if score >= 0.85:
        return TIER_HIGH
    if score >= 0.55:
        return TIER_MEDIUM
    if score >= 0.30:
        return TIER_LOW
    return TIER_NONE


def _base(action):
    """The registry key for a chosen action (strip any ':arg' suffix)."""
    return (action or "").split(":", 1)[0]


@dataclass(frozen=True)
class Grounding:
    tier: str                 # HIGH | MEDIUM | LOW | NONE
    grounded: bool            # tier is at least LOW (a supported, evidenced decision)
    score: float              # 0.0 .. 1.0 raw support score
    evidence_read: bool       # SigNoz evidence was consulted (or a verified replay)
    fingerprint_known: bool   # the incident maps to a known fingerprint class
    action_supported: bool    # the action addresses this SLO's fault signature
    hard_floor: bool          # a fixed-floor breach, not a statistical anomaly only
    reasons: tuple            # the ordered support/gap findings, human-readable

    def annotate(self, span):
        """Stamp the groundedness verdict onto the active span -> an audit trail in
        SigNoz, right next to the policy gate decision and the action."""
        if span is None:
            return
        span.set_attribute("heal.grounding.tier", self.tier)
        span.set_attribute("heal.grounding.grounded", self.grounded)
        span.set_attribute("heal.grounding.score", self.score)
        span.set_attribute("heal.grounding.evidence_read", self.evidence_read)
        span.set_attribute("heal.grounding.fingerprint_known", self.fingerprint_known)
        span.set_attribute("heal.grounding.action_supported", self.action_supported)
        span.set_attribute("heal.grounding.hard_floor", self.hard_floor)
        span.set_attribute("heal.grounding.reason", "; ".join(self.reasons))

    def line(self):
        return (f"[GROUNDING:{self.tier}] score={self.score:.2f} "
                f"grounded={self.grounded} -- " + "; ".join(self.reasons))


def audit(*, slo, action, decider="llm", evidence_read=False,
          fingerprint_known=False, hard_floor=True):
    """Score how well a chosen remediation is grounded in the incident evidence.

    ``slo`` is the breached SLO name; ``action`` the chosen remediation (any
    ``:arg`` suffix is ignored); ``decider`` is ``memory`` (verified replay),
    ``llm`` (the model chose) or ``fallback`` (a safe default was applied);
    ``evidence_read`` is whether ``read_incident`` was actually called;
    ``fingerprint_known`` whether the breach mapped to a known fingerprint class;
    ``hard_floor`` whether this was a fixed-floor breach (vs an anomaly only).

    Returns a :class:`Grounding`. The rules:

      * No action, or an action that does not address the SLO's fault signature,
        is ``NONE`` (the one hard gate: an unsupported fix is never confident).
      * Otherwise the support score buckets into a tier, then two caps apply: an
        anomaly-only signal is never above ``MEDIUM``; an unrecognised SLO (whose
        remediation set is unknown, so support cannot be verified) is never above
        ``LOW``.
    """
    base = _base(action)
    known_slo = slo in _REMEDIATION_FOR_SLO
    supported = base in _REMEDIATION_FOR_SLO.get(slo, set())
    verified_replay = decider == "memory"
    evidence = bool(evidence_read or verified_replay)

    if not base:
        return Grounding(TIER_NONE, False, 0.0, evidence, fingerprint_known,
                         False, hard_floor,
                         ("no remediation was applied, so there is nothing to ground",))

    # The single hard gate: an action that does not address this fault is ungrounded.
    if known_slo and not supported:
        return Grounding(
            TIER_NONE, False, 0.1, evidence, fingerprint_known, False, hard_floor,
            (f"action '{base}' does not address the {slo} fault signature "
             f"(remediations that do: {sorted(_REMEDIATION_FOR_SLO[slo])})",))

    reasons = []
    score = 0.0

    if verified_replay:
        reasons.append("replaying a SigNoz-verified fix, proven against real evidence")
        score += _W_EVIDENCE
    elif evidence_read:
        reasons.append("the decision consulted the live SigNoz incident evidence")
        score += _W_EVIDENCE
    else:
        reasons.append("the decision did NOT consult the incident evidence "
                       "(the fix was asserted, not grounded)")

    if supported:
        reasons.append(f"'{base}' is a remediation that targets the {slo} fault signature")
        score += _W_ACTION
    elif not known_slo:
        reasons.append(f"SLO '{slo}' has no known remediation set; support cannot be verified")

    if fingerprint_known:
        reasons.append("the breach maps to a known deterministic fingerprint class")
        score += _W_FINGERPRINT
    else:
        reasons.append("the breach did not map to a known fingerprint class")

    if hard_floor:
        score += _W_HARDFLOOR
    else:
        reasons.append("a statistical anomaly only, not a fixed-floor breach")

    tier = _bucket(score)
    # Caps: an anomaly-only signal is never HIGH; an unverifiable SLO is never above LOW.
    if not hard_floor and _TIER_RANK[tier] > _TIER_RANK[TIER_MEDIUM]:
        tier = TIER_MEDIUM
    if not known_slo and _TIER_RANK[tier] > _TIER_RANK[TIER_LOW]:
        tier = TIER_LOW

    grounded = _TIER_RANK[tier] >= _TIER_RANK[TIER_LOW]
    return Grounding(tier, grounded, round(score, 3), evidence, fingerprint_known,
                     supported, hard_floor, tuple(reasons))
