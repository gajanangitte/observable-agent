"""Unit tests for the AccessTrace SigNoz alert specs (no network).

These lock the two alert rule shapes and their contract with the runner: both are
v5 TRACES_BASED threshold rules that fire at_least_once above 0 on the accesstrace
spans, the critical one keys on an access.journey BREACH (the page), the warning one
keys on an access.step BREACH (the localiser), and the names are stable so --ensure
updates in place instead of duplicating. Importing the module needs no API key.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import access_alert as aa


def test_two_specs_registered():
    assert len(aa.ALERT_SPECS) == 2


def test_breach_spec_is_critical_journey_trigger():
    s = aa.breach_spec("5m", "local-webhook")
    assert s["alert"] == aa.BREACH_ALERT
    assert s["alertType"] == "TRACES_BASED_ALERT"
    assert s["version"] == "v5"
    assert s["labels"]["severity"] == "critical"
    assert s["labels"]["access_role"] == "trigger"
    filt = s["condition"]["compositeQuery"]["queries"][0]["spec"]["filter"]["expression"]
    assert "service.name = 'accesstrace'" in filt
    assert "name = 'access.journey'" in filt
    assert "a11y.status = 'BREACH'" in filt


def test_stage_spec_is_warning_step_notify():
    s = aa.stage_spec("5m", "local-webhook")
    assert s["alert"] == aa.STAGE_ALERT
    assert s["labels"]["severity"] == "warning"
    assert s["labels"]["access_role"] == "notify"
    filt = s["condition"]["compositeQuery"]["queries"][0]["spec"]["filter"]["expression"]
    assert "name = 'access.step'" in filt
    assert "a11y.status = 'BREACH'" in filt


def test_threshold_is_any_occurrence():
    for fn in aa.ALERT_SPECS:
        s = fn("5m", "ch")
        cond = s["condition"]
        assert cond["target"] == 0
        assert cond["op"] == "above"
        assert cond["matchType"] == "at_least_once"
        assert cond["compositeQuery"]["queries"][0]["spec"]["aggregations"] == [
            {"expression": "count()"}]


def test_alert_names_are_distinct_and_stable():
    assert aa.BREACH_ALERT != aa.STAGE_ALERT
    # the runner keys updates off these exact strings; guard against silent drift
    assert aa.BREACH_ALERT.startswith("AccessTrace WCAG Budget Breach")
    assert aa.STAGE_ALERT.startswith("AccessTrace Breaching Stage")


def test_channel_is_passed_through():
    s = aa.breach_spec("5m", "my-channel")
    assert s["preferredChannels"] == ["my-channel"]
    s2 = aa.breach_spec("5m", "")
    assert s2["preferredChannels"] == []
