"""Unit tests for heal_bridge pure logic (no network, no SigNoz):

  * _scenario_for -- map a fired alert to the incident the healer chases, and the
    critical guard that the notify-only backstop maps to None (a failed heal must
    never trigger another heal),
  * _alert_key -- stable per-alert cooldown key,
  * _incident_link -- turn the workload's .last_incident_trace handoff into an OTel
    span Link, with the staleness / zero-id / missing-file guards.

The trace file is redirected to a temp path so the real handoff is never touched.
"""
import json
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heal_alert as A
import heal_bridge as B

_TID = "0af7651916cd43dd8448eb211c80319c"
_SID = "b7ad6b7169203331"


def _write_incident(d):
    p = os.path.join(tempfile.gettempdir(), "heal_bridge_incident_test.json")
    with open(p, "w") as f:
        json.dump(d, f)
    B._INCIDENT_TRACE_FILE = pathlib.Path(p)
    return p


def test_scenario_maps_the_real_alert_names():
    # Tie the bridge's routing to the ACTUAL alert names so the two files cannot
    # drift apart: rename an alert and this test fails loudly.
    assert B._scenario_for(A.RETRY_ALERT) == "retry"
    assert B._scenario_for(A.COST_ALERT) == "cost"
    assert B._scenario_for(A.HEAL_ALERT) is None


def test_scenario_backstop_keywords_are_notify_only():
    for name in ["Self-Healer backstop", "an incident was not auto-resolved",
                 "SELF-HEALER anything"]:
        assert B._scenario_for(name) is None


def test_scenario_cost_keywords_route_to_cost():
    for name in ["Cost runaway", "monthly spend high", "bill shock", "over budget"]:
        assert B._scenario_for(name) == "cost"


def test_scenario_defaults_to_retry():
    assert B._scenario_for("some unknown alert") == "retry"
    assert B._scenario_for("") == "retry"
    assert B._scenario_for(None) == "retry"


def test_alert_key_prefers_id_then_name_then_unknown():
    assert B._alert_key("abc", "Name") == "abc"
    assert B._alert_key(None, "Name") == "Name"
    assert B._alert_key(0, "Name") == "Name"      # 0 is not a usable id
    assert B._alert_key(None, None) == "unknown"


def test_incident_link_valid():
    _write_incident({"trace_id": _TID, "span_id": _SID,
                     "service": "observable-agent", "ts": time.time()})
    link, hexid = B._incident_link()
    assert hexid == _TID
    assert link is not None
    assert link.context.trace_id == int(_TID, 16)
    assert link.context.span_id == int(_SID, 16)


def test_incident_link_stale_returns_none():
    # older than _INCIDENT_MAX_AGE_S (1800s) -> an unrelated past incident.
    _write_incident({"trace_id": _TID, "span_id": _SID,
                     "ts": time.time() - B._INCIDENT_MAX_AGE_S - 60})
    assert B._incident_link() == (None, None)


def test_incident_link_zero_ids_returns_none():
    _write_incident({"trace_id": "0" * 32, "span_id": "0" * 16, "ts": time.time()})
    assert B._incident_link() == (None, None)


def test_incident_link_missing_file_returns_none():
    B._INCIDENT_TRACE_FILE = pathlib.Path(
        os.path.join(tempfile.gettempdir(), "heal_bridge_incident_absent.json"))
    if B._INCIDENT_TRACE_FILE.exists():
        B._INCIDENT_TRACE_FILE.unlink()
    assert B._incident_link() == (None, None)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
