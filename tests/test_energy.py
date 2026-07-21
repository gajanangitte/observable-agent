"""Unit tests for the plug-and-play WattTrace energy model.

These lock in the GreenOps physics and its honesty guarantees with no network, no
SigNoz and no Ollama: the deterministic token-basis energy math (prefill and decode
throughput), the carbon and electricity conversions, the fail-closed north star
(joules per verified answer, UNKNOWN not zero when nothing verified), the three-state
budget verdict (PASS / BREACH / UNKNOWN below the minimum sample), the estimate
provenance stamps (a fallback is never labelled measured), and the layered config
resolution (built-in defaults, then energy.yaml, then WATT_* environment variables).

Every test that mutates env or the config path restores the default model in a
finally, so the module leaves no global state behind.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import energy

_ENV_KEYS = (
    "WATTTRACE_CONFIG", "WATT_BASIS", "WATT_DEFAULT_TIER", "WATT_DEFAULT_REGION",
    "WATT_PUE", "WATT_ELECTRICITY_USD_PER_KWH", "WATT_PREFILL_TPS", "WATT_DECODE_TPS",
    "WATT_ACTIVE_WATTS", "WATT_BUDGET_JOULES_PER_ANSWER", "WATT_BUDGET_GCO2_PER_ANSWER",
    "WATT_MIN_VERIFIED",
)


def _reset():
    """Drop any env/path overrides and rebuild from the shipped energy.yaml."""
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    energy.reload()


def _write_yaml(text):
    fh = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    fh.write(text)
    fh.close()
    return fh.name


# --- deterministic token-basis energy ----------------------------------------
def test_modelled_seconds_prefill_decode_split():
    _reset()
    en = energy._get()
    # Input is prefilled fast and in parallel; output is decoded one slow token at
    # a time. With the defaults (200 prefill, 8 decode) the two add up.
    assert abs(en.modelled_seconds(417, 14) - (417 / 200.0 + 14 / 8.0)) < 1e-9
    assert abs(en.modelled_seconds(400, 0) - 2.0) < 1e-9
    assert en.modelled_seconds(0, 0) == 0.0


def test_token_basis_is_deterministic_over_wall_time():
    _reset()
    # The whole point of the token basis: the joules do NOT move with a noisy CPU
    # wall clock. Same tokens, wildly different measured seconds, identical energy.
    a = energy.estimate(input_tokens=417, output_tokens=14, wall_seconds=0.5)
    b = energy.estimate(input_tokens=417, output_tokens=14, wall_seconds=44.0)
    assert a.joules == b.joules
    assert a.basis == "tokens"
    # 65 W desktop, PUE 1.0, 3.835 s modelled -> 249.275 J.
    assert abs(a.joules - 65.0 * (417 / 200.0 + 14 / 8.0)) < 1e-6
    # The measured wall time is still recorded as an independent cross-check.
    assert a.measured_seconds == 0.5 and b.measured_seconds == 44.0


def test_walltime_basis_uses_measured_and_falls_back():
    _reset()
    try:
        os.environ["WATT_BASIS"] = "walltime"
        energy.reload()
        e = energy.estimate(input_tokens=100, output_tokens=10, wall_seconds=5.0)
        assert e.basis == "walltime"
        assert abs(e.joules - 65.0 * 5.0 * 1.0) < 1e-6
        # No measured time available: walltime is impossible, so it falls back to
        # the deterministic token basis rather than inventing a number.
        e2 = energy.estimate(input_tokens=100, output_tokens=10, wall_seconds=None)
        assert e2.basis == "tokens"
    finally:
        _reset()


def test_carbon_and_electricity_conversions():
    _reset()
    en = energy._get()
    j = 3_600_000.0  # exactly 1 kWh
    # world grid default is 445 gCO2e/kWh, electricity 0.1341 USD/kWh.
    assert abs(en.grams_co2(j) - 445.0) < 1e-6
    assert abs(en.usd(j) - 0.1341) < 1e-6
    assert abs(en.wh(j) - 1000.0) < 1e-6
    assert abs(en.kwh(j) - 1.0) < 1e-9
    # Negative energy never yields negative money or carbon.
    assert en.grams_co2(-10.0) == 0.0 and en.usd(-10.0) == 0.0


def test_joules_per_token_unknown_on_zero():
    _reset()
    e = energy.estimate(input_tokens=417, output_tokens=14)
    assert abs(e.joules_per_token - e.joules / 431.0) < 1e-9
    # No tokens means joules per token is UNKNOWN, never a divide-by-zero or a zero.
    z = energy.estimate(input_tokens=0, output_tokens=0)
    assert z.joules_per_token is None


# --- the north star + fail-closed verdict ------------------------------------
def test_per_verified_is_unknown_not_zero_when_nothing_verified():
    _reset()
    # Zero verified answers is UNKNOWN (None), never zero energy per answer.
    assert energy.per_verified(1000.0, 0) is None
    assert energy.per_verified(900.0, 3) == 300.0


def test_verdict_pass_breach_and_unknown_below_minimum():
    _reset()
    # Below the minimum sample (3) the verdict is UNKNOWN regardless of energy,
    # so a tiny cohort can never fire a false breach nor a false all-clear.
    assert energy.verdict(10_000.0, 1.0, 2).status == energy.UNKNOWN
    assert energy.verdict(10.0, 0.001, 1).status == energy.UNKNOWN
    # At or above the sample it is a real pass/fail against the 900 J budget.
    ok = energy.verdict(2816.0, 0.34, 4)
    assert ok.status == energy.PASS and ok.joules_per_verified_answer == 704.0
    bad = energy.verdict(4256.0, 0.52, 4)
    assert bad.status == energy.BREACH and bad.joules_per_verified_answer == 1064.0
    # Zero verified is UNKNOWN (no denominator), not a silent healthy zero.
    assert energy.verdict(0.0, 0.0, 0).status == energy.UNKNOWN


def test_verdict_carries_gco2_per_answer_and_budget():
    _reset()
    v = energy.verdict(3600.0, 0.4, 4)
    assert abs(v.gco2_per_verified_answer - 0.1) < 1e-9
    assert v.budget_joules == 900.0
    assert v.verified == 4


# --- provenance honesty ------------------------------------------------------
def test_tier_provenance_quality_and_scope():
    _reset()
    en = energy._get()
    # The shipped default is a hardware proxy: an estimate, labelled as one.
    d = en.tier("cpu-desktop")
    assert d.method == "hardware_proxy" and d.quality == "ESTIMATED"
    assert d.scope == "cpu_package"
    # The generic fallback is never dressed up as anything better than FALLBACK.
    f = en.tier("generic-cpu-fallback")
    assert f.quality == "FALLBACK"
    # A GPU-server tier is scoped to the whole system, not a CPU package.
    assert en.tier("gpu-server").scope == "whole_system"


def test_estimate_stamps_the_tier_provenance():
    _reset()
    e = energy.estimate(input_tokens=100, output_tokens=10, tier="generic-cpu-fallback")
    assert e.method == "fallback" and e.quality == "FALLBACK"
    e2 = energy.estimate(input_tokens=100, output_tokens=10)
    assert e2.quality == "ESTIMATED" and e2.tier == "cpu-desktop"


def test_measured_watts_override_is_stamped_measured():
    _reset()
    try:
        # Feeding a real wall-meter reading marks the estimate as MEASURED and
        # overrides the tier's book value.
        os.environ["WATT_ACTIVE_WATTS"] = "90"
        energy.reload()
        e = energy.estimate(input_tokens=200, output_tokens=0)
        assert e.quality == "MEASURED"
        assert abs(e.watts - 90.0) < 1e-9
        assert abs(e.joules - 90.0 * (200 / 200.0)) < 1e-6
    finally:
        _reset()


# --- layered config: environment overrides -----------------------------------
def test_env_overrides_throughput_pue_budget_region():
    _reset()
    try:
        os.environ["WATT_PREFILL_TPS"] = "100"
        os.environ["WATT_DECODE_TPS"] = "10"
        os.environ["WATT_PUE"] = "2.0"
        os.environ["WATT_DEFAULT_REGION"] = "france"
        os.environ["WATT_BUDGET_JOULES_PER_ANSWER"] = "100"
        os.environ["WATT_MIN_VERIFIED"] = "1"
        energy.reload()
        en = energy._get()
        assert abs(en.modelled_seconds(100, 10) - 2.0) < 1e-9
        # PUE 2.0 doubles the energy: 65 W * 2 s * 2.0 = 260 J.
        assert abs(energy.estimate(input_tokens=100, output_tokens=10).joules - 260.0) < 1e-6
        # France grid is far cleaner than the world default.
        assert en.region().gco2_per_kwh < 100.0
        # A single verified answer now judges (min lowered) and 100 J budget breaches.
        assert energy.verdict(500.0, 0.05, 1).status == energy.BREACH
        assert energy.budget_joules_per_verified_answer() == 100.0
    finally:
        _reset()


# --- layered config: energy.yaml file override -------------------------------
def test_yaml_file_overrides_and_keeps_unspecified():
    _reset()
    path = _write_yaml(
        "hardware:\n"
        "  default_tier: test-rig\n"
        "  tiers:\n"
        "    test-rig:\n      active_watts: 100.0\n      source_kind: measured\n"
        "grid:\n"
        "  default_region: testland\n"
        "  regions:\n"
        "    testland:\n      gco2_per_kwh: 1000.0\n"
        "budget:\n  joules_per_verified_answer: 500.0\n")
    try:
        os.environ["WATTTRACE_CONFIG"] = path
        energy.reload()
        en = energy._get()
        # The new tier is selected and stamped from the file.
        assert en.tier().name == "test-rig"
        assert abs(en.tier().active_watts - 100.0) < 1e-9
        assert en.tier().quality == "MEASURED"
        # Dict-merge keeps the shipped tiers and regions that the file did not touch.
        assert abs(en.tier("cpu-desktop").active_watts - 65.0) < 1e-9
        assert "world" in en.regions
        # The new region and its dirty grid took effect.
        assert abs(en.region().gco2_per_kwh - 1000.0) < 1e-9
        # The tightened budget flips a run that would pass on the default 900 J.
        assert energy.verdict(2400.0, 0.3, 4).status == energy.BREACH  # 600 > 500
    finally:
        os.environ.pop("WATTTRACE_CONFIG", None)
        os.unlink(path)
        _reset()


def test_missing_config_file_falls_back_to_defaults():
    _reset()
    try:
        os.environ["WATTTRACE_CONFIG"] = os.path.join(
            tempfile.gettempdir(), "does-not-exist-watttrace.yaml")
        energy.reload()
        # No crash: the built-in defaults are in force.
        assert energy.budget_joules_per_verified_answer() == 900.0
        assert abs(energy._get().tier("cpu-desktop").active_watts - 65.0) < 1e-9
    finally:
        _reset()


def test_model_factor_scales_power():
    _reset()
    path = _write_yaml(
        "model_factor:\n  default: 1.0\n  mini: 0.5\n")
    try:
        os.environ["WATTTRACE_CONFIG"] = path
        energy.reload()
        en = energy._get()
        # A model whose id matches a factor key draws proportionally less power.
        assert abs(en.model_factor("gpt-4o-mini") - 0.5) < 1e-9
        assert abs(en.model_factor("llama3.2:3b") - 1.0) < 1e-9
        big = energy.estimate(input_tokens=200, output_tokens=0, model="llama3.2:3b").joules
        small = energy.estimate(input_tokens=200, output_tokens=0, model="gpt-4o-mini").joules
        assert abs(small - big * 0.5) < 1e-6
    finally:
        os.environ.pop("WATTTRACE_CONFIG", None)
        os.unlink(path)
        _reset()


# --- impact report (dash-free, basis-aware) ----------------------------------
def test_impact_report_lines_and_basis_note():
    _reset()
    v = energy.verdict(4256.0, 0.52, 4)  # a BREACH
    r = energy.impact_report(verdict=v, wasted_joules=1375.0, quality="ESTIMATED")
    text = " ".join(r["lines"])
    assert "per verified answer" in text
    assert "Wasted" in text and "percent of the run" in text
    # The token basis must be described honestly, not as a hardware reading.
    assert "token counts" in text
    assert "not read from a hardware sensor" in text
    # No dashes in the money-and-carbon prose (user style directive).
    for ln in r["lines"]:
        assert "\u2014" not in ln and "\u2013" not in ln and " - " not in ln


def test_impact_report_walltime_basis_note():
    _reset()
    try:
        os.environ["WATT_BASIS"] = "walltime"
        energy.reload()
        v = energy.verdict(4256.0, 0.52, 4)
        r = energy.impact_report(verdict=v, wasted_joules=100.0)
        assert any("wall clock" in ln for ln in r["lines"])
    finally:
        _reset()


def test_impact_report_unknown_verdict_is_honest():
    _reset()
    v = energy.verdict(100.0, 0.01, 1)  # below minimum -> UNKNOWN
    r = energy.impact_report(verdict=v)
    assert any("UNKNOWN" in ln for ln in r["lines"])


def test_verdict_zero_energy_is_unknown_not_a_comforting_pass():
    _reset()
    # Enough verified answers, but the energy accounting produced nothing (e.g. an
    # OpenAI-compatible endpoint returned no token usage, so every call modelled to
    # 0 J). A zero-joule reading is an accounting failure, not free work, so the
    # verdict must be UNKNOWN, never the green "comforting zero" the track forbids.
    v = energy.verdict(0.0, 0.0, 5)
    assert v.status == energy.UNKNOWN
    assert v.joules_per_verified_answer is None
    assert "no energy" in v.reason


def test_verdict_breaches_on_carbon_even_when_joules_pass():
    _reset()
    # Joules per answer are within the 900 J budget, but carbon per answer is over
    # the 0.11 gCO2e budget: the configurable carbon knob must actually bite.
    # 3200 J over 4 answers = 800 J/answer (< 900). 0.60 g over 4 = 0.15 g (> 0.11).
    v = energy.verdict(3200.0, 0.60, 4)
    assert v.joules_per_verified_answer == 800.0
    assert v.gco2_per_verified_answer == 0.15
    assert v.status == energy.BREACH
    assert "gCO2e" in v.reason
    # A run under both budgets still passes.
    ok = energy.verdict(3200.0, 0.30, 4)
    assert ok.status == energy.PASS


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    _reset()
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
