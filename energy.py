"""Plug-and-play GreenOps energy and carbon model for the agent (WattTrace).

Everything about ENERGY and CARBON lives here (and in ``energy.yaml``): how many
watts your machine draws while a model is generating, how dirty your grid is,
what a kWh costs, and the GreenOps budget the alert and the CI gate enforce. A
company plugs in its OWN numbers by editing ``energy.yaml`` or setting a few
``WATT_*`` environment variables. No code changes, no redeploy. Plug and play.

Resolution order, lowest to highest precedence (identical shape to economics.py):

  1. the built-in defaults in this file (real, sourced, dated public figures),
     so a fresh clone, an offline box, and the test suite all just work.
  2. ``energy.yaml`` (path via the ``WATTTRACE_CONFIG`` env var, default the repo
     file), the single file you edit to make the model yours.
  3. individual ``WATT_*`` environment variables, for quick per-run overrides.

HONESTY: WattTrace ESTIMATES energy from active power draw times active compute
time (by default the time implied by the token counts, not a raw wall clock).
It is a power model, not a hardware sensor. Every estimate is stamped with its
provenance (``method`` and ``quality``) so a generic fallback is never presented
as a measurement. If you feed in a wall meter or RAPL reading, the same estimate
is stamped MEASURED. This mirrors CodeCarbon's fallback methodology (85W TDP at
50 percent when no sensor is available).

Design notes: NO network calls, fully deterministic, does NOT import ``config``,
``telemetry`` or ``agent`` (so any of them may import this without a cycle).
YAML is optional: if PyYAML is missing or the file is absent, the built-in
defaults are used, so nothing ever hard-fails.
"""
from __future__ import annotations

import copy
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:  # dotenv is already a dependency; load defensively for direct imports.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is always present in this project
    pass

_JOULES_PER_KWH = 3_600_000.0

# Verdict states for the GreenOps SLO (fail closed, three-state like the sensors).
PASS = "PASS"
BREACH = "BREACH"
UNKNOWN = "UNKNOWN"

# Estimate quality, folded from a tier's source_kind. Never let a fallback read
# as a measurement.
_KIND_QUALITY = {
    "measured": "MEASURED",
    "configured": "ESTIMATED",
    "hardware_proxy": "ESTIMATED",
    "fallback": "FALLBACK",
}


# --- Built-in defaults (mirror energy.yaml; the offline-safe source) ----------
_DEFAULTS: dict = {
    "hardware": {
        "default_tier": "cpu-desktop",
        "fallback_tier": "generic-cpu-fallback",
        "tiers": {
            "generic-cpu-fallback": {
                "active_watts": 42.5, "source_kind": "fallback",
                "note": "CodeCarbon generic CPU fallback: 85W TDP times 50 percent utilisation"},
            "cpu-desktop": {
                "active_watts": 65.0, "source_kind": "hardware_proxy",
                "note": "typical desktop CPU package under sustained load (65W class TDP)"},
            "cpu-laptop": {
                "active_watts": 28.0, "source_kind": "hardware_proxy",
                "note": "typical laptop CPU package under sustained load (28W class TDP)"},
            "gpu-consumer": {
                "active_watts": 200.0, "source_kind": "hardware_proxy",
                "note": "consumer inference GPU board power (RTX 3060/4060 class)"},
            "gpu-server": {
                "active_watts": 350.0, "source_kind": "hardware_proxy",
                "note": "datacenter inference GPU board power (A100/L40 class)"},
            "calibrated-example": {
                "active_watts": 71.0, "source_kind": "measured",
                "note": "example of a wall-meter reading you took during a real run"},
        },
    },
    "pue": 1.0,
    "grid": {
        "default_region": "world",
        "regions": {
            "world":   {"gco2_per_kwh": 445.0,   "year": 2024,
                        "source": "Ember Global Electricity Review 2025; retrieved 2026-07-21"},
            "us":      {"gco2_per_kwh": 369.473, "year": 2023,
                        "source": "Our World in Data / CodeCarbon energy mix; retrieved 2026-07-21"},
            "eu":      {"gco2_per_kwh": 251.0,   "year": 2023,
                        "source": "EEA 2023 EU-27 average; retrieved 2026-07-21"},
            "india":   {"gco2_per_kwh": 713.441, "year": 2023,
                        "source": "Our World in Data / CodeCarbon energy mix; retrieved 2026-07-21"},
            "germany": {"gco2_per_kwh": 380.950, "year": 2023,
                        "source": "Our World in Data / CodeCarbon energy mix; retrieved 2026-07-21"},
            "france":  {"gco2_per_kwh": 56.039,  "year": 2023,
                        "source": "Our World in Data / CodeCarbon energy mix (nuclear heavy); retrieved 2026-07-21"},
            "norway":  {"gco2_per_kwh": 30.0,    "year": 2023,
                        "source": "Ember 2023 Norway (hydro); retrieved 2026-07-21"},
        },
    },
    "electricity_usd_per_kwh": 0.1341,
    "model_factor": {"default": 1.0},
    # Steady-state inference throughput used to MODEL a call's active time from
    # its token counts (prefill runs the input in parallel and is fast; decode
    # emits output one token at a time and is slow). Modelling the time from
    # tokens rather than trusting a raw wall clock keeps the energy figure
    # deterministic and reproducible, immune to cold-start and CPU contention
    # noise. These are typical figures for a 3B model on a desktop CPU; measure
    # your own and override them.
    "throughput": {"prefill_tokens_per_sec": 200.0, "decode_tokens_per_sec": 8.0},
    # How a call's active time is derived: "tokens" (deterministic, the default)
    # or "walltime" (the raw measured duration, physically exact but noisy on a
    # shared CPU). Either way energy = active_watts times time times PUE.
    "basis": "tokens",
    "budget": {
        "joules_per_verified_answer": 900.0,
        "gco2_per_verified_answer": 0.11,
        "minimum_verified_answers": 3,
    },
}


# --- typed model -------------------------------------------------------------
@dataclass(frozen=True)
class HardwareTier:
    """Active power draw for one hardware tier, and how that figure was obtained."""
    name: str
    active_watts: float
    source_kind: str = "hardware_proxy"
    note: str = ""

    @property
    def method(self) -> str:
        return self.source_kind

    @property
    def quality(self) -> str:
        return _KIND_QUALITY.get(self.source_kind, "ESTIMATED")

    @property
    def scope(self) -> str:
        # CPU tiers price the package; anything else is treated as whole-system.
        return "cpu_package" if self.name.lower().startswith("cpu") else "whole_system"


@dataclass(frozen=True)
class GridRegion:
    """Carbon intensity of one electricity grid, in grams CO2e per kWh."""
    name: str
    gco2_per_kwh: float
    year: int = 0
    source: str = ""


@dataclass(frozen=True)
class Budget:
    """The GreenOps SLO the alert and CI gate enforce."""
    joules_per_verified_answer: float
    gco2_per_verified_answer: float
    minimum_verified_answers: int


@dataclass(frozen=True)
class CallEstimate:
    """The energy footprint of one inference call, with full provenance."""
    joules: float
    grams_co2: float
    usd: float
    watts: float
    modelled_seconds: float
    measured_seconds: Optional[float]
    input_tokens: int
    output_tokens: int
    tokens: int
    joules_per_token: Optional[float]
    tier: str
    region: str
    basis: str
    method: str
    quality: str
    scope: str


@dataclass(frozen=True)
class Verdict:
    """Three-state GreenOps SLO result over a cohort."""
    status: str
    joules_per_verified_answer: Optional[float]
    gco2_per_verified_answer: Optional[float]
    verified: int
    total_joules: float
    total_grams: float
    budget_joules: float
    reason: str = ""


@dataclass
class Energy:
    """The resolved energy model. Build it with :func:`load` or :func:`reload`."""
    tiers: Dict[str, HardwareTier]
    default_tier: str
    fallback_tier: str
    regions: Dict[str, GridRegion]
    default_region: str
    pue: float
    electricity_usd_per_kwh: float
    model_factors: Dict[str, float]
    prefill_tps: float
    decode_tps: float
    basis: str
    budget: Budget

    # -- lookups --------------------------------------------------------------
    def tier(self, name: Optional[str] = None) -> HardwareTier:
        key = (name or self.default_tier or "").strip()
        t = self.tiers.get(key)
        if t is None:
            t = self.tiers.get(self.default_tier)
        if t is None:
            t = self.tiers.get(self.fallback_tier)
        if t is None:
            # Last resort so callers never crash on a misconfigured file.
            t = HardwareTier(name=self.fallback_tier or "fallback",
                             active_watts=42.5, source_kind="fallback",
                             note="built-in last-resort fallback")
        return t

    def region(self, name: Optional[str] = None) -> GridRegion:
        key = (name or self.default_region or "").strip()
        r = self.regions.get(key)
        if r is None:
            r = self.regions.get(self.default_region)
        if r is None:
            r = GridRegion(name=key or "unknown", gco2_per_kwh=445.0,
                           source="built-in fallback")
        return r

    def model_factor(self, model: Optional[str]) -> float:
        m = (model or "").lower()
        for key, val in self.model_factors.items():
            if key != "default" and key.lower() in m:
                return float(val)
        return float(self.model_factors.get("default", 1.0))

    # -- energy math ----------------------------------------------------------
    def active_watts(self, tier: Optional[str] = None,
                     model: Optional[str] = None) -> float:
        return self.tier(tier).active_watts * self.model_factor(model)

    def modelled_seconds(self, input_tokens: int, output_tokens: int) -> float:
        """Deterministic active time from token counts: prefill the input in
        parallel, decode the output one token at a time."""
        pre = max(1e-9, self.prefill_tps)
        dec = max(1e-9, self.decode_tps)
        return max(0, int(input_tokens or 0)) / pre + max(0, int(output_tokens or 0)) / dec

    def joules(self, seconds: float, tier: Optional[str] = None,
               model: Optional[str] = None) -> float:
        """IT energy times PUE. joules = watts * seconds * pue."""
        secs = max(0.0, float(seconds))
        return self.active_watts(tier, model) * secs * self.pue

    def kwh(self, joules: float) -> float:
        return max(0.0, float(joules)) / _JOULES_PER_KWH

    def wh(self, joules: float) -> float:
        return max(0.0, float(joules)) / 3600.0

    def grams_co2(self, joules: float, region: Optional[str] = None) -> float:
        return self.kwh(joules) * self.region(region).gco2_per_kwh

    def usd(self, joules: float) -> float:
        return self.kwh(joules) * self.electricity_usd_per_kwh

    def joules_per_token(self, joules: float, tokens: int) -> Optional[float]:
        # Missing tokens make J/token UNKNOWN; they never block the energy figure.
        if not tokens or tokens <= 0:
            return None
        return max(0.0, float(joules)) / float(tokens)

    def estimate(self, input_tokens: int = 0, output_tokens: int = 0,
                 wall_seconds: Optional[float] = None, tier: Optional[str] = None,
                 model: Optional[str] = None, region: Optional[str] = None,
                 basis: Optional[str] = None) -> CallEstimate:
        """Bundle the full, provenance-stamped footprint of one inference call.

        The headline ``joules`` uses the configured basis: "tokens" models the
        active time from token counts (deterministic, the default) and
        "walltime" uses the measured duration. The measured wall time is always
        recorded too, so the two can be compared.
        """
        t = self.tier(tier)
        r = self.region(region)
        in_tok = max(0, int(input_tokens or 0))
        out_tok = max(0, int(output_tokens or 0))
        tokens = in_tok + out_tok
        b = (basis or self.basis or "tokens").lower()
        modelled_s = self.modelled_seconds(in_tok, out_tok)
        measured_s = None if wall_seconds is None else max(0.0, float(wall_seconds))
        if b == "walltime" and measured_s is not None:
            secs = measured_s
        else:
            b = "tokens"
            secs = modelled_s
        j = self.joules(secs, tier=t.name, model=model)
        return CallEstimate(
            joules=j,
            grams_co2=self.grams_co2(j, region=r.name),
            usd=self.usd(j),
            watts=self.active_watts(t.name, model),
            modelled_seconds=modelled_s,
            measured_seconds=measured_s,
            input_tokens=in_tok,
            output_tokens=out_tok,
            tokens=tokens,
            joules_per_token=self.joules_per_token(j, tokens),
            tier=t.name,
            region=r.name,
            basis=b,
            method=t.method,
            quality=t.quality,
            scope=t.scope,
        )

    # -- north-star + SLO -----------------------------------------------------
    def per_verified(self, total_joules: float, verified: int) -> Optional[float]:
        """Joules per verified answer. None (UNKNOWN) when there are none, so a
        run with zero verified answers is never reported as zero energy cost."""
        if not verified or verified <= 0:
            return None
        return max(0.0, float(total_joules)) / float(verified)

    def verdict(self, total_joules: float, total_grams: float,
                verified: int) -> Verdict:
        """Fail-closed GreenOps SLO. UNKNOWN below the minimum sample so a tiny
        cohort never fires a false breach nor a false all-clear, and UNKNOWN (never
        a comforting PASS) when answers verified but no energy was recorded: a
        zero-joule reading means the accounting failed, not that the work was free.
        A cohort BREACHES if it is over EITHER the joule budget or the carbon
        budget per verified answer, so the configurable carbon knob actually bites.
        """
        b = self.budget
        jpa = self.per_verified(total_joules, verified)
        gpa = (max(0.0, float(total_grams)) / verified) if verified > 0 else None
        if verified < b.minimum_verified_answers:
            return Verdict(UNKNOWN, jpa, gpa, verified, total_joules, total_grams,
                           b.joules_per_verified_answer,
                           reason=f"only {verified} verified answers "
                                  f"(need {b.minimum_verified_answers} to judge)")
        if not math.isfinite(total_joules) or total_joules <= 0:
            return Verdict(UNKNOWN, None, gpa, verified, total_joules, total_grams,
                           b.joules_per_verified_answer,
                           reason=f"{verified} verified answers but no energy was "
                                  f"recorded (missing token counts, a non-finite figure, "
                                  f"or an accounting failure); refusing a zero all-clear")
        over_j = jpa is not None and jpa > b.joules_per_verified_answer
        over_c = (gpa is not None and b.gco2_per_verified_answer > 0
                  and gpa > b.gco2_per_verified_answer)
        status = BREACH if (over_j or over_c) else PASS
        if over_j and over_c:
            reason = (f"{jpa:.0f} J (budget {b.joules_per_verified_answer:.0f} J) and "
                      f"{gpa:.3f} gCO2e (budget {b.gco2_per_verified_answer:.3f}) per "
                      f"verified answer")
        elif over_c:
            reason = (f"{gpa:.3f} gCO2e per verified answer over the "
                      f"{b.gco2_per_verified_answer:.3f} budget (energy {jpa:.0f} J "
                      f"within its {b.joules_per_verified_answer:.0f} J budget)")
        else:
            reason = (f"{jpa:.0f} J per verified answer vs budget "
                      f"{b.joules_per_verified_answer:.0f} J")
        return Verdict(status, jpa, gpa, verified, total_joules, total_grams,
                       b.joules_per_verified_answer, reason=reason)

    # -- relatable equivalents (dash-free prose) -----------------------------
    def phone_charges(self, joules: float) -> float:
        """How many smartphone charges this energy equals. A full phone battery
        is about 19 kJ (about 12 Wh); a comforting, real-world yardstick."""
        return max(0.0, float(joules)) / 19_000.0

    def impact_report(self, *, verdict: Optional[Verdict] = None,
                      wasted_joules: float = 0.0, region: Optional[str] = None,
                      quality: str = "ESTIMATED") -> dict:
        """A dash-free money-and-carbon summary of a WattTrace run."""
        out: dict = {"lines": []}
        r = self.region(region)
        if verdict is None:
            return out
        v = verdict
        total_wh = self.wh(v.total_joules)
        if v.joules_per_verified_answer is not None:
            out["lines"].append(
                f"Energy per verified answer: {v.joules_per_verified_answer:.0f} J "
                f"({self.wh(v.joules_per_verified_answer):.2f} Wh), "
                f"budget {v.budget_joules:.0f} J. Verdict {v.status}.")
        else:
            out["lines"].append(
                f"Energy per verified answer: UNKNOWN ({v.reason}).")
        out["lines"].append(
            f"Run total: {v.total_joules:.0f} J ({total_wh:.2f} Wh), "
            f"{v.total_grams:.3f} g CO2e on the {r.name} grid "
            f"({r.gco2_per_kwh:.0f} g per kWh), "
            f"about {self.usd(v.total_joules):.6f} USD of electricity.")
        if wasted_joules > 0 and v.total_joules > 0:
            share = 100.0 * wasted_joules / v.total_joules
            out["lines"].append(
                f"Wasted on retries and failed work: {wasted_joules:.0f} J "
                f"({share:.0f} percent of the run), "
                f"{self.grams_co2(wasted_joules, r.name):.3f} g CO2e for zero extra "
                f"verified answers.")
        basis_note = ("the compute time implied by the token counts (prefill and "
                      "decode throughput)" if (self.basis or "tokens") == "tokens"
                      else "measured wall clock time")
        out["lines"].append(
            f"Estimate quality: {quality}. Energy is modelled from active power "
            f"draw times {basis_note}, not read from a hardware sensor.")
        return out


# --- merge + build (identical shape to economics.py) -------------------------
def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_yaml(path: str) -> Optional[dict]:
    if not path or not os.path.isfile(path):
        return None
    try:
        import yaml  # optional dependency
    except Exception:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # energy.yaml nests everything under a top-level "watttrace" key; accept
    # both the nested and a flat form so either layout works.
    if "watttrace" in data and isinstance(data["watttrace"], dict):
        return data["watttrace"]
    return data


def _config_path() -> str:
    env = os.getenv("WATTTRACE_CONFIG")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "energy.yaml")


def _f(env_name: str, current):
    raw = os.getenv(env_name)
    if raw is None or raw == "":
        return current
    try:
        return float(raw)
    except ValueError:
        return current


def _apply_env(raw: dict) -> dict:
    raw = copy.deepcopy(raw)
    raw.setdefault("hardware", {})
    raw.setdefault("grid", {})
    raw.setdefault("budget", {})
    tier = os.getenv("WATT_DEFAULT_TIER")
    if tier:
        raw["hardware"]["default_tier"] = tier
    region = os.getenv("WATT_DEFAULT_REGION")
    if region:
        raw["grid"]["default_region"] = region
    raw["pue"] = _f("WATT_PUE", raw.get("pue", 1.0))
    raw["electricity_usd_per_kwh"] = _f(
        "WATT_ELECTRICITY_USD_PER_KWH", raw.get("electricity_usd_per_kwh", 0.1341))
    basis = os.getenv("WATT_BASIS")
    if basis:
        raw["basis"] = basis.lower()
    raw.setdefault("throughput", {})
    raw["throughput"]["prefill_tokens_per_sec"] = _f(
        "WATT_PREFILL_TPS", raw["throughput"].get("prefill_tokens_per_sec", 200.0))
    raw["throughput"]["decode_tokens_per_sec"] = _f(
        "WATT_DECODE_TPS", raw["throughput"].get("decode_tokens_per_sec", 8.0))
    # A measured wall-clock power reading overrides the chosen tier's watts and
    # is stamped as a real measurement.
    watts = os.getenv("WATT_ACTIVE_WATTS")
    if watts:
        try:
            w = float(watts)
            tiers = raw["hardware"].setdefault("tiers", {})
            dt = raw["hardware"].get("default_tier", "cpu-desktop")
            tiers[dt] = {**(tiers.get(dt, {})), "active_watts": w,
                         "source_kind": "measured",
                         "note": "overridden via WATT_ACTIVE_WATTS"}
        except ValueError:
            pass
    raw["budget"]["joules_per_verified_answer"] = _f(
        "WATT_BUDGET_JOULES_PER_ANSWER",
        raw["budget"].get("joules_per_verified_answer", 900.0))
    raw["budget"]["gco2_per_verified_answer"] = _f(
        "WATT_BUDGET_GCO2_PER_ANSWER",
        raw["budget"].get("gco2_per_verified_answer", 0.11))
    minv = os.getenv("WATT_MIN_VERIFIED")
    if minv:
        try:
            raw["budget"]["minimum_verified_answers"] = int(float(minv))
        except ValueError:
            pass
    return raw


def _build(raw: dict) -> Energy:
    hw = raw.get("hardware", {}) or {}
    tiers: Dict[str, HardwareTier] = {}
    for name, spec in (hw.get("tiers", {}) or {}).items():
        try:
            tiers[name] = HardwareTier(
                name=name, active_watts=float(spec.get("active_watts", 42.5)),
                source_kind=str(spec.get("source_kind", "hardware_proxy")),
                note=str(spec.get("note", "")))
        except (TypeError, ValueError):
            continue
    if not tiers:
        tiers["generic-cpu-fallback"] = HardwareTier(
            "generic-cpu-fallback", 42.5, "fallback", "built-in fallback")

    grid = raw.get("grid", {}) or {}
    regions: Dict[str, GridRegion] = {}
    for name, spec in (grid.get("regions", {}) or {}).items():
        try:
            regions[name] = GridRegion(
                name=name, gco2_per_kwh=float(spec.get("gco2_per_kwh", 445.0)),
                year=int(spec.get("year", 0) or 0), source=str(spec.get("source", "")))
        except (TypeError, ValueError):
            continue
    if not regions:
        regions["world"] = GridRegion("world", 445.0, 2024, "built-in fallback")

    mf_raw = raw.get("model_factor", {}) or {"default": 1.0}
    model_factors: Dict[str, float] = {}
    for k, val in mf_raw.items():
        try:
            model_factors[k] = float(val)
        except (TypeError, ValueError):
            continue
    model_factors.setdefault("default", 1.0)

    tp = raw.get("throughput", {}) or {}
    prefill_tps = float(tp.get("prefill_tokens_per_sec", 200.0) or 200.0)
    decode_tps = float(tp.get("decode_tokens_per_sec", 8.0) or 8.0)
    basis = str(raw.get("basis", "tokens") or "tokens").lower()

    b = raw.get("budget", {}) or {}
    budget = Budget(
        joules_per_verified_answer=float(b.get("joules_per_verified_answer", 900.0)),
        gco2_per_verified_answer=float(b.get("gco2_per_verified_answer", 0.11)),
        minimum_verified_answers=int(b.get("minimum_verified_answers", 3) or 3),
    )

    return Energy(
        tiers=tiers,
        default_tier=str(hw.get("default_tier", "cpu-desktop")),
        fallback_tier=str(hw.get("fallback_tier", "generic-cpu-fallback")),
        regions=regions,
        default_region=str(grid.get("default_region", "world")),
        pue=float(raw.get("pue", 1.0)),
        electricity_usd_per_kwh=float(raw.get("electricity_usd_per_kwh", 0.1341)),
        model_factors=model_factors,
        prefill_tps=prefill_tps,
        decode_tps=decode_tps,
        basis=basis,
        budget=budget,
    )


def load() -> Energy:
    """Resolve the full energy model: defaults, then energy.yaml, then env."""
    raw = _deep_merge(_DEFAULTS, _load_yaml(_config_path()) or {})
    raw = _apply_env(raw)
    return _build(raw)


# --- module-level singleton + convenience API --------------------------------
_ENERGY: Optional[Energy] = None


def _install(en: Energy) -> Energy:
    global _ENERGY
    _ENERGY = en
    return en


def _get() -> Energy:
    return _ENERGY if _ENERGY is not None else _install(load())


def reload() -> Energy:
    """Rebuild from disk + current env (used by tests after changing env)."""
    return _install(load())


def joules(wall_seconds: float, tier: Optional[str] = None,
           model: Optional[str] = None) -> float:
    return _get().joules(wall_seconds, tier, model)


def grams_co2(joules_value: float, region: Optional[str] = None) -> float:
    return _get().grams_co2(joules_value, region)


def usd(joules_value: float) -> float:
    return _get().usd(joules_value)


def estimate(input_tokens: int = 0, output_tokens: int = 0,
             wall_seconds: Optional[float] = None, tier: Optional[str] = None,
             model: Optional[str] = None, region: Optional[str] = None,
             basis: Optional[str] = None) -> CallEstimate:
    return _get().estimate(input_tokens, output_tokens, wall_seconds, tier,
                           model, region, basis)


def per_verified(total_joules: float, verified: int) -> Optional[float]:
    return _get().per_verified(total_joules, verified)


def verdict(total_joules: float, total_grams: float, verified: int) -> Verdict:
    return _get().verdict(total_joules, total_grams, verified)


def impact_report(**kwargs) -> dict:
    return _get().impact_report(**kwargs)


def budget_joules_per_verified_answer() -> float:
    return _get().budget.joules_per_verified_answer


def budget_gco2_per_verified_answer() -> float:
    return _get().budget.gco2_per_verified_answer


def minimum_verified_answers() -> int:
    return _get().budget.minimum_verified_answers
