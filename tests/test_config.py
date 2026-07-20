"""Unit tests for config: tiered model routing + cost/pricing math.

These lock in the "why a local model?" answer -- the agent is local-first and only
reaches a cloud tier when one is fully configured -- and the illustrative cost
model. No network, no SigNoz, no Ollama.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


def test_tier_local_is_the_configured_endpoint():
    t = config.tier("local")
    assert t["name"] == "local"
    assert t["base_url"] == config.GENAI_BASE_URL
    assert t["api_key"] == config.GENAI_API_KEY
    assert t["model"] == config.MODEL
    assert t["system"] == config.GENAI_SYSTEM


def test_tier_escalation_falls_back_to_local_when_unconfigured():
    # Default posture: no cloud tier configured -> asking for escalation resolves to
    # local, so the agent stays fully offline and callers never need to pre-check.
    saved = config.ESCALATION_ENABLED
    config.ESCALATION_ENABLED = False
    try:
        t = config.tier("escalation")
        assert t["name"] == "local"
        assert t["base_url"] == config.GENAI_BASE_URL
    finally:
        config.ESCALATION_ENABLED = saved


def test_tier_escalation_uses_cloud_when_configured():
    saved = (config.ESCALATION_ENABLED, config.ESCALATION_BASE_URL,
             config.ESCALATION_API_KEY, config.ESCALATION_MODEL, config.ESCALATION_SYSTEM)
    config.ESCALATION_ENABLED = True
    config.ESCALATION_BASE_URL = "https://api.example.com/v1"
    config.ESCALATION_API_KEY = "sk-test"
    config.ESCALATION_MODEL = "gpt-4o"
    config.ESCALATION_SYSTEM = "openai"
    try:
        t = config.tier("escalation")
        assert t["name"] == "escalation"
        assert t["base_url"] == "https://api.example.com/v1"
        assert t["api_key"] == "sk-test"
        assert t["model"] == "gpt-4o"
        assert t["system"] == "openai"
    finally:
        (config.ESCALATION_ENABLED, config.ESCALATION_BASE_URL,
         config.ESCALATION_API_KEY, config.ESCALATION_MODEL,
         config.ESCALATION_SYSTEM) = saved


def test_tier_unknown_name_resolves_to_local():
    assert config.tier("anything-else")["name"] == "local"


def test_price_for_matches_model_family():
    assert config.price_for("qwen2.5:3b") == config.PRICING["qwen2.5"]
    assert config.price_for("llama3.2:3b") == config.PRICING["llama3.2"]
    # The longer, more specific key must not shadow the base model and vice versa.
    assert config.price_for("gpt-4o") == config.PRICING["gpt-4o"]
    assert config.price_for("gpt-4o-mini") == config.PRICING["gpt-4o-mini"]


def test_price_for_unknown_falls_back_to_default():
    assert config.price_for("totally-unknown-model") == config.PRICING["default"]
    assert config.price_for("") == config.PRICING["default"]
    assert config.price_for(None) == config.PRICING["default"]


def test_cost_usd_math():
    p_in, p_out = config.price_for("qwen2.5")
    # 1,000,000 in + 1,000,000 out == exactly (p_in + p_out) dollars.
    c = config.cost_usd("qwen2.5", 1_000_000, 1_000_000)
    assert abs(c - (p_in + p_out)) < 1e-9
    # Asymmetric pricing is applied to the right token side.
    gi, go = config.price_for("gpt-4o")
    assert abs(config.cost_usd("gpt-4o", 1_000_000, 0) - gi) < 1e-9
    assert abs(config.cost_usd("gpt-4o", 0, 1_000_000) - go) < 1e-9
    assert config.cost_usd("qwen2.5", 0, 0) == 0.0


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
