"""Unit tests for heal_policy: the name gate and the argument validator."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heal_policy as hp


def test_unknown_action_never_allowed():
    for level in hp.AUTONOMY_LEVELS:
        d = hp.Policy(autonomy=level).evaluate("rm_minus_rf")
        assert d.allow is False


def test_auto_allows_low_risk_reversible():
    d = hp.Policy(autonomy="auto").evaluate("disable_fault_injection")
    assert d.allow is True


def test_auto_holds_medium_risk():
    # switch_model is medium risk -> above the default 'low' auto cap -> held
    d = hp.Policy(autonomy="auto").evaluate("switch_model")
    assert d.allow is False
    assert d.requires_approval is True


def test_suggest_never_applies():
    d = hp.Policy(autonomy="suggest").evaluate("disable_fault_injection")
    assert d.allow is False


def test_validate_params_accepts_in_bounds():
    ok, _why, clean = hp.validate_params("set_cost_budget", {"usd": 0.0005})
    assert ok is True
    assert clean["usd"] == 0.0005


def test_validate_params_rejects_absurd_budget():
    ok, why, _ = hp.validate_params("set_cost_budget", {"usd": 1000000.0})
    assert ok is False
    assert "outside" in why


def test_validate_params_rejects_non_numeric_budget():
    ok, _why, _ = hp.validate_params("set_cost_budget", {"usd": "lots"})
    assert ok is False


def test_validate_params_unset_uses_default():
    ok, _why, clean = hp.validate_params("set_cost_budget", {"usd": None})
    assert ok is True
    assert "usd" not in clean          # omitted -> actuator falls back to its default


def test_validate_params_model_enum():
    assert hp.validate_params("switch_model", {"to": "llama3.2:1b"})[0] is True
    assert hp.validate_params("switch_model", {"to": "gpt-4o"})[0] is False


def test_validate_params_unbounded_action_passthrough():
    ok, _why, clean = hp.validate_params("disable_fault_injection", {"anything": 1})
    assert ok is True
    assert clean == {"anything": 1}


def test_invalid_autonomy_fails_closed_to_observe():
    # A typo or unknown value must drop to the safest mode, never silently grant the
    # most permissive one. Fail closed: an unreadable setting cannot escalate power.
    p = hp.Policy(autonomy="AUTOMATIC-TYPO")
    assert p.autonomy == "observe"
    assert p.evaluate("disable_fault_injection").allow is False
    # A valid explicit level is still honoured verbatim.
    assert hp.Policy(autonomy="auto").autonomy == "auto"


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
