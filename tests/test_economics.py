"""Unit tests for the plug-and-play economics model.

These lock in the money layer: token pricing (family match + specific-first
ordering), cost math, cost of downtime per named profile, the real-money impact
report, and the layered config resolution (built-in defaults, then economics.yaml,
then environment variables) plus the config.* back-compat surface. No network, no
SigNoz, no Ollama. Every test that mutates env or the config path restores the
default model in a finally, so the module leaves no global state behind.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import economics
import config


def _reset():
    """Drop any env/path overrides and rebuild from built-in defaults."""
    for k in ("ECONOMICS_CONFIG", "ECON_ACTIVE_DOWNTIME_PROFILE",
              "ECON_REQUESTS_PER_DAY", "ECON_BILLING_MODEL",
              "ECON_COST_MAX_CALLS_PER_REQ", "ECON_NOMINAL_CALL_COST_USD",
              "ECON_PER_REQUEST_BUDGET_USD"):
        os.environ.pop(k, None)
    economics.reload()


# --- pricing -----------------------------------------------------------------
def test_price_for_matches_model_family():
    _reset()
    assert economics.price_for("qwen2.5:3b") == (0.10, 0.10)
    assert economics.price_for("llama3.2:3b") == (0.10, 0.10)
    assert economics.price_for("claude-3-5-sonnet-latest") == (3.00, 15.00)
    assert economics.price_for("gemini-1.5-flash-002") == (0.075, 0.30)


def test_price_for_specific_before_general_ordering():
    _reset()
    # gpt-4o-mini must win over gpt-4o for a mini id, and gpt-4o for a plain id.
    assert economics.price_for("gpt-4o-mini") == (0.15, 0.60)
    assert economics.price_for("gpt-4o-2024-08-06") == (2.50, 10.00)
    assert economics.price_for("gpt-4o") == (2.50, 10.00)


def test_price_for_unknown_falls_back_to_default():
    _reset()
    assert economics.price_for("totally-unknown-model") == (0.10, 0.10)
    assert economics.price_for("") == (0.10, 0.10)
    assert economics.price_for(None) == (0.10, 0.10)


def test_cost_usd_math():
    _reset()
    # 1e6 in + 1e6 out == exactly (p_in + p_out) dollars.
    assert abs(economics.cost_usd("gpt-4o", 1_000_000, 1_000_000) - 12.50) < 1e-9
    # Asymmetric pricing hits the right token side.
    assert abs(economics.cost_usd("gpt-4o", 1_000_000, 0) - 2.50) < 1e-9
    assert abs(economics.cost_usd("gpt-4o", 0, 1_000_000) - 10.00) < 1e-9
    assert economics.cost_usd("qwen2.5", 0, 0) == 0.0


# --- downtime ----------------------------------------------------------------
def test_downtime_cost_uses_active_profile_by_default():
    _reset()
    # Default active profile is the Gartner baseline: 5,600 USD/min.
    assert economics.downtime_cost_usd(10) == 56000.0
    assert economics.downtime_cost_usd(0) == 0.0


def test_downtime_cost_named_profile_and_bad_input():
    _reset()
    assert economics.downtime_cost_usd(1, "itic_regulated_2024") == 16667.0
    assert economics.downtime_cost_usd(1, "itic_enterprise_2024") == 5000.0
    # Negative minutes clamp to zero.
    assert economics.downtime_cost_usd(-5) == 0.0
    # An unknown profile name falls back to the active profile, never crashes.
    assert economics.downtime_cost_usd(1, "does-not-exist") == 5600.0


# --- impact report -----------------------------------------------------------
def test_impact_report_spend_and_downtime_lines():
    _reset()
    r = economics.impact_report(spend_before=0.000600, spend_after=0.000100,
                                mttr_s=120, requests_per_day=100000)
    # 0.0005/req saved * 100000 req/day * 30 days == 1500 USD/month.
    assert abs(r["monthly_spend_saved_usd"] - 1500.0) < 1e-6
    assert abs(r["per_request_saved_usd"] - 0.0005) < 1e-9
    assert r["outage_cost_usd"] == 56000.0
    assert len(r["lines"]) == 2
    assert any("per month" in ln for ln in r["lines"])
    assert any("0 humans paged" in ln for ln in r["lines"])


def test_impact_report_no_spend_line_when_not_cheaper():
    _reset()
    r = economics.impact_report(spend_before=0.0001, spend_after=0.0002, mttr_s=30)
    assert "monthly_spend_saved_usd" not in r
    # Downtime line still present (it does not depend on spend).
    assert any("outage" in ln for ln in r["lines"])


# --- layered config: economics.yaml file override ----------------------------
def test_yaml_file_overrides_defaults_and_keeps_unspecified():
    _reset()
    try:
        # Only override the default price, budget, downtime and volume. The
        # built-in model catalog is a dict sibling, so it must survive.
        y = ("pricing:\n"
             "  default:\n    input: 9.0\n    output: 9.0\n"
             "downtime:\n  active_profile: itic_regulated_2024\n"
             "requests_per_day: 50000\n"
             "budget:\n  per_request_usd: 0.002\n")
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(y)
            path = fh.name
        os.environ["ECONOMICS_CONFIG"] = path
        economics.reload()
        # Built-in models kept (dict merge), default replaced.
        assert economics.price_for("gpt-4o") == (2.50, 10.00)
        assert economics.price_for("unknown") == (9.0, 9.0)
        # Downtime active profile + budget + volume all took effect.
        assert economics.downtime_cost_usd(1) == 16667.0
        assert economics.default_budget_usd() == 0.002
        assert economics._get().requests_per_day == 50000
    finally:
        os.environ.pop("ECONOMICS_CONFIG", None)
        os.unlink(path)
        _reset()


def test_yaml_models_list_replaces_and_orders():
    _reset()
    try:
        y = ("pricing:\n"
             "  default:\n    input: 0.10\n    output: 0.10\n"
             "  models:\n"
             "    - match: acme-mini\n      input: 1.0\n      output: 2.0\n"
             "    - match: acme\n      input: 5.0\n      output: 6.0\n")
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(y)
            path = fh.name
        os.environ["ECONOMICS_CONFIG"] = path
        economics.reload()
        # A supplied models list fully replaces the built-in one.
        assert economics.price_for("gpt-4o") == (0.10, 0.10)   # gone -> default
        # Specific-first ordering from the file is honored.
        assert economics.price_for("acme-mini-v2") == (1.0, 2.0)
        assert economics.price_for("acme-pro") == (5.0, 6.0)
    finally:
        os.environ.pop("ECONOMICS_CONFIG", None)
        os.unlink(path)
        _reset()


def test_missing_config_file_falls_back_to_defaults():
    _reset()
    try:
        os.environ["ECONOMICS_CONFIG"] = os.path.join(
            tempfile.gettempdir(), "does-not-exist-economics.yaml")
        economics.reload()
        # No crash; built-in defaults in force.
        assert economics.price_for("gpt-4o") == (2.50, 10.00)
        assert economics.default_budget_usd() == 0.0001
    finally:
        _reset()


# --- layered config: environment variable overrides --------------------------
def test_env_overrides_win_over_file_and_defaults():
    _reset()
    try:
        os.environ["ECON_ACTIVE_DOWNTIME_PROFILE"] = "itic_enterprise_2024"
        os.environ["ECON_REQUESTS_PER_DAY"] = "1000000"
        os.environ["ECON_PER_REQUEST_BUDGET_USD"] = "0.005"
        os.environ["ECON_COST_MAX_CALLS_PER_REQ"] = "9"
        os.environ["ECON_NOMINAL_CALL_COST_USD"] = "0.001"
        economics.reload()
        assert economics.downtime_cost_usd(1) == 5000.0
        assert economics._get().requests_per_day == 1000000
        assert economics.default_budget_usd() == 0.005
        assert economics.cost_slo_max_calls_per_request() == 9.0
        assert economics.nominal_call_cost_usd() == 0.001
    finally:
        _reset()


# --- back-compat: config.* re-exports ----------------------------------------
def test_config_reexports_match_economics():
    _reset()
    assert config.price_for("gpt-4o") == economics.price_for("gpt-4o")
    assert config.cost_usd("gpt-4o", 1000, 2000) == economics.cost_usd("gpt-4o", 1000, 2000)
    # config.PRICING keeps the historical shape: a "default" key plus families,
    # with the specific id ordered before the general one.
    assert config.PRICING["default"] == (0.10, 0.10)
    assert config.PRICING["gpt-4o"] == (2.50, 10.00)
    keys = list(config.PRICING.keys())
    assert keys.index("gpt-4o-mini") < keys.index("gpt-4o")


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    _reset()
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
