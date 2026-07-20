"""Unit tests for heal_alert: the three SLO-aligned alert specs.

Guards the exact v5 shape the SigNoz alerts API needs AND the invariant that the
alert thresholds ARE the healer's SLO constants (single source of truth), so an
alert can never silently drift away from the sensor it is meant to mirror. The
spec builders touch no network, so this imports and runs on a fresh clone.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heal_alert as A
import heal_sensors as S


def _queries(spec):
    return spec["condition"]["compositeQuery"]["queries"]


def test_retry_alert_is_a_ratio_on_the_retry_slo():
    spec = A.retry_spec("5m", "local-webhook")
    assert spec["alert"] == A.RETRY_ALERT
    cond = spec["condition"]
    assert cond["target"] == S.RETRY_SLO_MAX_RATE      # threshold == the sensor SLO
    assert cond["op"] == "above"
    assert cond["selectedQueryName"] == "F1"           # fire on the ratio, not a raw count
    qs = _queries(spec)
    assert [q.get("type") for q in qs] == ["builder_query", "builder_query", "builder_formula"]
    a, b, f = qs
    assert a["spec"]["disabled"] is True and b["spec"]["disabled"] is True
    assert f["spec"]["expression"] == "A/B"
    # numerator = retried llm.chat, denominator = all llm.chat
    assert "response_dropped" in a["spec"]["filter"]["expression"]
    assert "retry.reason" not in b["spec"]["filter"]["expression"]
    assert spec["labels"]["heal_role"] == "trigger"
    assert spec["labels"]["slo"] == "retry_tax"


def test_cost_alert_is_a_ratio_on_the_cost_slo():
    spec = A.cost_spec("5m", "local-webhook")
    assert spec["alert"] == A.COST_ALERT
    cond = spec["condition"]
    assert cond["target"] == S.COST_SLO_MAX_CALLS_PER_REQ
    assert cond["op"] == "above"
    assert cond["selectedQueryName"] == "F1"
    a, b, f = _queries(spec)
    assert f["spec"]["expression"] == "A/B"
    assert "llm.chat" in a["spec"]["filter"]["expression"]        # numerator = calls
    assert "agent.invoke" in b["spec"]["filter"]["expression"]    # denominator = requests
    assert spec["labels"]["heal_role"] == "trigger"
    assert spec["labels"]["slo"] == "cost_runaway"


def test_backstop_alert_is_notify_only():
    spec = A.heal_spec("5m", "local-webhook")
    assert spec["alert"] == A.HEAL_ALERT
    # heal_role=notify is what stops a failed heal from launching another heal.
    assert spec["labels"]["heal_role"] == "notify"
    assert spec["labels"]["severity"] == "critical"
    qs = _queries(spec)
    assert len(qs) == 1 and qs[0]["type"] == "builder_query"      # no ratio formula
    assert spec["condition"]["selectedQueryName"] == "A"
    expr = qs[0]["spec"]["filter"]["expression"]
    assert "self-healer" in expr and "agent.heal" in expr and "heal.healed = false" in expr


def test_every_spec_carries_channel_and_v5_version():
    for fn in A.ALERT_SPECS:
        spec = fn("5m", "local-webhook")
        # SigNoz 400s a rule POST with no notification channel.
        assert spec["preferredChannels"] == ["local-webhook"]
        assert spec["version"] == "v5"


def test_alert_names_are_the_three_stable_identities():
    names = {fn("5m", "c")["alert"] for fn in A.ALERT_SPECS}
    assert names == {A.RETRY_ALERT, A.COST_ALERT, A.HEAL_ALERT}


def test_missing_channel_yields_empty_channel_list():
    assert A.retry_spec("5m", "")["preferredChannels"] == []


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
