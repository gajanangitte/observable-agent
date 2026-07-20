"""Offline tests that the actuators enforce the policy gate AND argument bounds,
using a fake control plane so nothing touches heal_state.json or the network."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heal_actuators
import heal_policy


class FakeControls:
    def __init__(self):
        self.state = {}

    def save(self):
        return self.state


def _build(actions):
    decisions, gate_log = [], []
    schemas, registry = heal_actuators.build(
        mcp=None, controls=FakeControls(), cohort="c", decisions=decisions,
        actions=actions, policy=heal_policy.Policy(autonomy="auto"), gate_log=gate_log)
    return registry, decisions


def test_cost_budget_in_bounds_applies():
    registry, _ = _build(("set_cost_budget", "switch_model"))
    res = registry["set_cost_budget"](usd=0.0005)
    assert res["applied"] is True
    assert res["cost_budget_usd"] == 0.0005


def test_cost_budget_absurd_is_rejected():
    registry, decisions = _build(("set_cost_budget", "switch_model"))
    res = registry["set_cost_budget"](usd=1_000_000.0)
    assert res["applied"] is False
    assert res["rejected"] is True
    # a rejected action must NOT consume the remediation (model can try another)
    assert "set_cost_budget" not in decisions


def test_cost_budget_default_when_unset():
    registry, _ = _build(("set_cost_budget",))
    res = registry["set_cost_budget"]()
    assert res["applied"] is True
    assert res["cost_budget_usd"] == heal_actuators.HEAL_COST_BUDGET_USD


def test_switch_model_unknown_is_rejected():
    registry, _ = _build(("switch_model",))
    # switch_model is medium risk -> held under the default auto cap (name gate),
    # so first prove the name gate holds it...
    res = registry["switch_model"](to="gpt-4o")
    assert res["applied"] is False


def test_switch_model_enum_rejected_when_approved():
    # approve switch_model past the name gate, then a bad model must still fail
    # on the ARGUMENT bound.
    decisions, gate_log = [], []
    _schemas, registry = heal_actuators.build(
        mcp=None, controls=FakeControls(), cohort="c", decisions=decisions,
        actions=("switch_model",),
        policy=heal_policy.Policy(autonomy="auto", approved=["switch_model"]),
        gate_log=gate_log)
    bad = registry["switch_model"](to="totally-not-a-model")
    assert bad["applied"] is False and bad["rejected"] is True
    good = registry["switch_model"](to="llama3.2:1b")
    assert good["applied"] is True and good["model"] == "llama3.2:1b"


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
