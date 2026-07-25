"""Unit tests for chaos.py (the named fault catalog).

Network-free. The pure surface (FAULTS table, describe, plan, env_for) is tested
directly; inject/clear/armed/status are tested against a fake Controls so the real
heal_state.json is never touched.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chaos
from heal_controls import HEALTHY


class FakeControls:
    """A Controls stand-in: an in-memory state dict + a save() that no-ops (never
    touches heal_state.json)."""

    def __init__(self, state=None):
        self.state = dict(state or HEALTHY)
        self.saved = 0

    def save(self):
        self.saved += 1
        return self.state


def test_catalog_nonempty_and_stable():
    names = chaos.list_faults()
    assert "response_drop" in names
    assert "runaway_loop" in names
    assert "carbon_waste" in names
    # Stable order (dict insertion order preserved).
    assert chaos.list_faults() == list(chaos.FAULTS.keys())


def test_get_unknown_raises_with_catalog():
    try:
        chaos.get("no_such_fault")
    except KeyError as e:
        assert "known faults" in str(e)
    else:
        raise AssertionError("expected KeyError for an unknown fault")


def test_describe_is_serialisable():
    d = chaos.describe("response_drop")
    assert d["name"] == "response_drop"
    assert d["slo"] == "retry_tax"
    assert "disable_fault_injection" in d["remediations"]
    # round-trips through JSON (all primitives / lists / dicts)
    import json
    assert json.loads(json.dumps(d))["name"] == "response_drop"


def test_every_fault_maps_to_real_knobs():
    # Every catalogued fault's control keys must be real control-plane keys, and
    # its remediations must be real policy actions -- no cosmetic entries.
    import heal_policy
    for name in chaos.list_faults():
        f = chaos.get(name)
        for k in f.control:
            assert k in HEALTHY, f"{name}: control key {k} is not a real control-plane knob"
        for action in f.remediations:
            assert action in heal_policy.ACTION_POLICIES, \
                f"{name}: remediation {action} is not a known policy action"
        assert f.heal_scenario in ("retry", "cost", "carbon")


def test_plan_merges_onto_healthy():
    p = chaos.plan("response_drop")
    assert p["chaos_drop"] is True
    assert p["mitigation"] is False
    # untouched knobs keep their healthy defaults
    assert p["runaway"] == HEALTHY["runaway"]
    assert p["model"] == HEALTHY["model"]


def test_plan_runaway():
    p = chaos.plan("runaway_loop")
    assert p["runaway"] is True
    assert p["chaos_drop"] == HEALTHY["chaos_drop"]


def test_env_for_reproduces_fault():
    assert chaos.env_for("response_drop") == {"CHAOS_DROP_RESPONSE_ONCE": "1"}
    assert chaos.env_for("runaway_loop") == {"CHAOS_RUNAWAY": "1"}


def test_inject_arms_and_saves():
    c = FakeControls()
    state = chaos.inject("response_drop", c)
    assert state["chaos_drop"] is True
    assert c.saved == 1
    assert "response_drop" in chaos.armed(c)


def test_carbon_and_response_share_injection_point():
    # carbon_waste and response_drop both arm chaos_drop, so both read as armed
    # when either is injected -- they are the same physical fault, two SLO lenses.
    c = FakeControls()
    chaos.inject("carbon_waste", c)
    hits = chaos.armed(c)
    assert "response_drop" in hits
    assert "carbon_waste" in hits


def test_clear_returns_to_healthy():
    c = FakeControls()
    chaos.inject("runaway_loop", c)
    chaos.inject("response_drop", c)
    chaos.clear(c)
    assert c.state["chaos_drop"] == HEALTHY["chaos_drop"]
    assert c.state["runaway"] == HEALTHY["runaway"]
    assert c.state["mitigation"] == HEALTHY["mitigation"]
    assert chaos.armed(c) == []


def test_armed_empty_on_healthy():
    c = FakeControls()
    assert chaos.armed(c) == []


def test_cli_list_and_describe():
    # _main returns 0 and prints; drive it directly (no subprocess). Suppress its
    # stdout so the test-suite output stays clean.
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        assert chaos._main(["list"]) == 0
        assert chaos._main(["describe", "runaway_loop"]) == 0
