"""Policy governance for the healer's actions -- the gate between *decide* and *act*.

Detecting and diagnosing an incident is cheap and safe. *Acting* on a running
workload is not. This module is what turns the healer from "an agent that can
change config" into "an agent that can change config **within policy**": every
proposed remediation is evaluated -- against an autonomy level, an action
allow-list, and each action's declared blast radius / reversibility -- BEFORE it
is applied. The decision is recorded on the trace, so every action carries an
auditable "why was this allowed (or held)?" record next to it.

Autonomy levels (least -> most autonomous), via ``HEAL_AUTONOMY``:

  observe  -- never act. Detect + diagnose only (read-only).
  suggest  -- name the remediation but do not apply it (a human applies it).
  approve  -- apply only actions that are explicitly approved
              (``HEAL_APPROVED_ACTIONS``); hold everything else for a human.
  auto     -- apply any allow-listed, reversible action whose risk is within
              ``HEAL_AUTO_MAX_RISK``; STILL hold anything riskier for approval.

This is "policy as code": the same allow-list + blast-radius table an SRE team
would put in front of *any* automated remediation, expressed once and enforced at
the point of action.
"""
import os
from dataclasses import dataclass

# The action allow-list. An action the model chooses that is NOT here can never be
# applied, whatever the autonomy level -- an agent cannot invent new powers.
ACTION_POLICIES = {
    "disable_fault_injection": {
        "risk": "low", "reversible": True, "blast_radius": "service",
        "summary": "Remove an injected fault at its source."},
    "enable_mitigation": {
        "risk": "low", "reversible": True, "blast_radius": "service",
        "summary": "Enable an idempotency guard that compensates for the fault."},
    "set_cost_budget": {
        "risk": "low", "reversible": True, "blast_radius": "service",
        "summary": "Cap per-request LLM spend; a runtime circuit-breaker enforces it."},
    "switch_model": {
        "risk": "medium", "reversible": True, "blast_radius": "service",
        "summary": "Route the workload to a different model."},
}

RISK_ORDER = {"low": 1, "medium": 2, "high": 3}
AUTONOMY_LEVELS = ("observe", "suggest", "approve", "auto")

# Per-action ARGUMENT bounds. ``ACTION_POLICIES`` above answers "may this action
# run at all?"; this answers "are the arguments it was called with safe?". An
# out-of-range dollar budget or an unknown model is rejected exactly like a
# disallowed action -- so the model cannot, say, set a $1,000,000 "budget" (which
# would disable the very cost breaker it is arming) or route the workload to an
# arbitrary/unavailable model.
PARAM_SPECS = {
    "set_cost_budget": {
        "usd": {"type": "float", "min": 0.00001, "max": 1.0},
    },
    "switch_model": {
        "to": {"type": "enum", "allowed": ["llama3.2:1b", "llama3.2:3b", "qwen2.5:3b"]},
    },
}


def validate_params(action, params):
    """Validate/sanitise an action's arguments against ``PARAM_SPECS``.

    Returns ``(ok, reason, clean)``. ``clean`` contains only coerced, in-bounds
    values; an argument left unset is simply omitted (the actuator falls back to
    its own safe default). An out-of-bounds or wrong-type argument fails closed.
    """
    spec = PARAM_SPECS.get(action)
    params = params or {}
    if not spec:
        return True, "no bounded parameters", dict(params)
    clean = {}
    for name, rule in spec.items():
        if name not in params or params[name] is None:
            continue                       # unset -> actuator uses its safe default
        val = params[name]
        if rule["type"] == "float":
            try:
                val = float(val)
            except (TypeError, ValueError):
                return False, f"{name}={params[name]!r} is not a number", {}
            if val < rule["min"] or val > rule["max"]:
                return False, f"{name}={val:g} is outside [{rule['min']:g}, {rule['max']:g}]", {}
            clean[name] = val
        elif rule["type"] == "enum":
            if val not in rule["allowed"]:
                return False, f"{name}={val!r} is not one of {rule['allowed']}", {}
            clean[name] = val
    return True, "arguments within bounds", clean


@dataclass
class Decision:
    action: str
    autonomy: str
    allow: bool
    requires_approval: bool
    approved: bool
    risk: str
    reversible: bool
    blast_radius: str
    reason: str

    def annotate(self, span):
        """Stamp the gate decision onto the active span -> an audit trail in SigNoz."""
        if span is None:
            return
        span.set_attribute("heal.policy.autonomy", self.autonomy)
        span.set_attribute("heal.policy.action", self.action)
        span.set_attribute("heal.policy.allow", self.allow)
        span.set_attribute("heal.policy.requires_approval", self.requires_approval)
        span.set_attribute("heal.policy.approved", self.approved)
        span.set_attribute("heal.policy.risk", self.risk)
        span.set_attribute("heal.policy.reversible", self.reversible)
        span.set_attribute("heal.policy.blast_radius", self.blast_radius)
        span.set_attribute("heal.policy.reason", self.reason)

    def line(self):
        verb = "ALLOW" if self.allow else ("HOLD" if self.requires_approval else "DENY")
        return (f"[POLICY:{self.autonomy}] {verb} {self.action} "
                f"(risk={self.risk}, reversible={self.reversible}, "
                f"blast={self.blast_radius}) -- {self.reason}")


class Policy:
    """Evaluates a proposed remediation against the governance rules."""

    def __init__(self, autonomy=None, auto_max_risk=None, approved=None):
        self.autonomy = (autonomy or os.getenv("HEAL_AUTONOMY", "auto")).strip().lower()
        if self.autonomy not in AUTONOMY_LEVELS:
            self.autonomy = "auto"
        self.auto_max_risk = (auto_max_risk
                              or os.getenv("HEAL_AUTO_MAX_RISK", "low")).strip().lower()
        if self.auto_max_risk not in RISK_ORDER:
            self.auto_max_risk = "low"
        if approved is None:
            raw = os.getenv("HEAL_APPROVED_ACTIONS", "")
            approved = [a.strip() for a in raw.split(",") if a.strip()]
        self.approved = set(approved)

    def evaluate(self, action: str) -> Decision:
        meta = ACTION_POLICIES.get(action)
        if meta is None:
            return Decision(action, self.autonomy, allow=False, requires_approval=False,
                            approved=False, risk="unknown", reversible=False,
                            blast_radius="unknown",
                            reason="action is not in the policy allow-list")
        risk, reversible, blast = meta["risk"], meta["reversible"], meta["blast_radius"]
        approved = action in self.approved

        if self.autonomy == "observe":
            return Decision(action, self.autonomy, False, False, approved, risk,
                            reversible, blast,
                            "observe mode: detection and diagnosis only, no actions")
        if self.autonomy == "suggest":
            return Decision(action, self.autonomy, False, False, approved, risk,
                            reversible, blast,
                            "suggest mode: remediation proposed for a human to apply")
        if self.autonomy == "approve":
            if approved:
                return Decision(action, self.autonomy, True, False, True, risk,
                                reversible, blast, "explicitly approved")
            return Decision(action, self.autonomy, False, True, False, risk,
                            reversible, blast,
                            "approve mode: awaiting human approval before acting")
        # auto
        within = RISK_ORDER[risk] <= RISK_ORDER[self.auto_max_risk]
        if within and reversible:
            return Decision(action, self.autonomy, True, False, approved, risk,
                            reversible, blast,
                            f"auto: risk '{risk}' within cap '{self.auto_max_risk}' and reversible")
        if approved:
            return Decision(action, self.autonomy, True, False, True, risk,
                            reversible, blast,
                            "above auto-risk cap but explicitly approved")
        why = "irreversible" if not reversible else f"risk '{risk}' exceeds cap '{self.auto_max_risk}'"
        return Decision(action, self.autonomy, False, True, False, risk, reversible,
                        blast, f"{why}: held for human approval")

    def summary(self):
        return (f"autonomy={self.autonomy}, auto_max_risk={self.auto_max_risk}, "
                f"pre-approved={sorted(self.approved) or 'none'}")
