"""The healer's own OpenTelemetry instruments.

Created lazily via ``init()`` AFTER ``telemetry.setup_telemetry()`` has installed
the global MeterProvider, so these metrics flow to SigNoz on the ``self-healer``
service alongside the healer's ``agent.heal`` traces.
"""
from opentelemetry import metrics

_m = {}


def init():
    meter = metrics.get_meter("self-healer")
    _m["breach"] = meter.create_counter(
        "heal.slo.breach", unit="{breach}",
        description="SLO breaches the healer detected in SigNoz")
    _m["action"] = meter.create_counter(
        "heal.action", unit="{action}",
        description="Remediation actions the healer applied")
    _m["result"] = meter.create_counter(
        "heal.result", unit="{heal}",
        description="Heal outcomes (healed=true/false)")
    _m["mttr"] = meter.create_histogram(
        "heal.mttr", unit="ms",
        description="Mean time to repair: breach detected -> verified healed")
    _m["rate"] = meter.create_histogram(
        "heal.retry_rate", unit="1",
        description="Observed retry-tax rate per cohort (0..1), pre vs post heal")
    _m["policy"] = meter.create_counter(
        "heal.policy", unit="{decision}",
        description="Policy-gate decisions on proposed remediations")
    _m["rollback"] = meter.create_counter(
        "heal.rollback", unit="{rollback}",
        description="Remediations rolled back after a failed verify")
    _m["decision"] = meter.create_counter(
        "heal.decision", unit="{decision}",
        description="How each remediation was decided: memory | llm | fallback | human")
    _m["recall"] = meter.create_counter(
        "heal.recall", unit="{lookup}",
        description="Verified-memory recall lookups, labelled hit or miss")
    _m["unsafe"] = meter.create_counter(
        "heal.unsafe_action", unit="{action}",
        description="Actions applied that violated policy or param bounds (must stay 0)")
    _m["spend"] = meter.create_histogram(
        "heal.cost.spend", unit="USD",
        description="Observed per-request LLM spend per cohort, pre vs post heal")
    _m["cpr"] = meter.create_histogram(
        "heal.cost.calls_per_request", unit="1",
        description="Observed llm.chat calls per request per cohort, pre vs post heal")


def breach(slo, cohort):
    _m["breach"].add(1, {"slo": slo, "cohort": cohort})


def action(name):
    _m["action"].add(1, {"action": name})


def result(slo, healed):
    _m["result"].add(1, {"slo": slo, "healed": str(bool(healed)).lower()})


def mttr(ms, slo):
    _m["mttr"].record(ms, {"slo": slo})


def retry_rate(rate, cohort, phase):
    _m["rate"].record(rate, {"cohort": cohort, "phase": phase})


def policy(decision, action):
    _m["policy"].add(1, {"decision": decision, "action": action})


def rollback(action):
    _m["rollback"].add(1, {"action": action})


def decision(source, action):
    _m["decision"].add(1, {"source": source, "action": action or "none"})


def recall(result, fingerprint_class):
    _m["recall"].add(1, {"result": result, "fingerprint": fingerprint_class})


def unsafe_action(action, reason):
    _m["unsafe"].add(1, {"action": action, "reason": reason})


def cost_spend(usd, cohort, phase):
    _m["spend"].record(usd, {"cohort": cohort, "phase": phase})


def calls_per_request(value, cohort, phase):
    _m["cpr"].record(value, {"cohort": cohort, "phase": phase})
