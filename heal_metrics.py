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
