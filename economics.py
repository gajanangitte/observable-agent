"""Plug-and-play cost and economics model for the self-healing agent.

Everything about MONEY lives here (and in ``economics.yaml``): what a token
costs, what a minute of downtime costs your business, the cost SLO, and the
spend budget the circuit breaker arms. A company plugs in its OWN numbers by
editing ``economics.yaml`` or setting a few environment variables. No code
changes, no redeploy. Plug and play.

Resolution order, lowest to highest precedence:

  1. the built-in defaults in this file (real, sourced, dated public figures),
     so a fresh clone, an offline box, and the test suite all just work.
  2. ``economics.yaml`` (path via the ``ECONOMICS_CONFIG`` env var, default the
     repo file), the single file you edit to make the model yours.
  3. individual environment variables, for quick per-run overrides.

Design notes: this module has NO network calls, is fully deterministic, and does
NOT import ``config`` (``config`` imports THIS, so the dependency is one way).
YAML is optional: if PyYAML is not installed or the file is missing, the
built-in defaults are used, so nothing ever hard-fails.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:  # dotenv is already a dependency; load defensively for direct imports.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is always present in this project
    pass

# --- Built-in defaults (mirror economics.yaml; the offline-safe source) -------
# These are real public list prices and downtime benchmarks with sources and a
# retrieved date. They are duplicated here on purpose so the model works with
# ZERO config; economics.yaml, when present, overrides them.
_DEFAULTS: dict = {
    "pricing": {
        "default": {"input": 0.10, "output": 0.10},
        "models": [
            {"match": "llama3.2", "input": 0.10, "output": 0.10,
             "provider": "local/ollama",
             "source": "illustrative (local inference is free); retrieved 2026-07-21"},
            {"match": "llama3.1", "input": 0.10, "output": 0.10,
             "provider": "local/ollama",
             "source": "illustrative (local inference is free); retrieved 2026-07-21"},
            {"match": "qwen2.5", "input": 0.10, "output": 0.10,
             "provider": "local/ollama",
             "source": "illustrative (local inference is free); retrieved 2026-07-21"},
            {"match": "gpt-4o-mini", "input": 0.15, "output": 0.60,
             "provider": "openai",
             "source": "openai.com/api/pricing; retrieved 2026-07-21"},
            {"match": "gpt-4o", "input": 2.50, "output": 10.00,
             "provider": "openai",
             "source": "openai.com/api/pricing; retrieved 2026-07-21"},
            {"match": "claude-3-5-sonnet", "input": 3.00, "output": 15.00,
             "provider": "anthropic",
             "source": "anthropic.com/pricing; retrieved 2026-07-21"},
            {"match": "claude-3-5-haiku", "input": 0.80, "output": 4.00,
             "provider": "anthropic",
             "source": "anthropic.com/pricing; retrieved 2026-07-21"},
            {"match": "gemini-1.5-pro", "input": 1.25, "output": 5.00,
             "provider": "google",
             "source": "ai.google.dev/pricing (<=128K context tier); retrieved 2026-07-21"},
            {"match": "gemini-1.5-flash", "input": 0.075, "output": 0.30,
             "provider": "google",
             "source": "ai.google.dev/pricing (<=128K context tier); retrieved 2026-07-21"},
        ],
    },
    "downtime": {
        "active_profile": "gartner_baseline",
        "profiles": {
            "gartner_baseline": {
                "usd_per_minute": 5600,
                "label": "Gartner baseline (about 336K USD per hour)",
                "source": "Gartner, widely cited 5,600 USD/min; retrieved 2026-07-21"},
            "itic_enterprise_2024": {
                "usd_per_minute": 5000,
                "label": "ITIC 2024 enterprise (about 300K USD per hour)",
                "source": "ITIC 2024: 90 percent of enterprises exceed 300K USD/hr; retrieved 2026-07-21"},
            "itic_regulated_2024": {
                "usd_per_minute": 16667,
                "label": "ITIC 2024 regulated (about 1M USD per hour)",
                "source": "ITIC 2024: 41 percent report 1M to 5M USD/hr (low end); retrieved 2026-07-21"},
            "custom": {
                "usd_per_minute": 0,
                "label": "plug in your own cost of downtime",
                "source": "set this to your finance team's number"},
        },
    },
    "billing_model": "gpt-4o-mini",
    "requests_per_day": 100000,
    "slo": {
        "cost_max_calls_per_request": 6,
        "nominal_call_cost_usd": 0.00004,
    },
    "budget": {
        "per_request_usd": 0.0001,
    },
}


# --- Typed views over the config ---------------------------------------------
@dataclass(frozen=True)
class ModelPrice:
    """A per-model token price. ``input``/``output`` are USD per 1e6 tokens."""
    match: str
    input: float
    output: float
    provider: str = ""
    source: str = ""

    @property
    def tuple(self) -> Tuple[float, float]:
        return (self.input, self.output)


@dataclass(frozen=True)
class DowntimeProfile:
    """What a minute of downtime costs, for one named business profile."""
    name: str
    usd_per_minute: float
    label: str = ""
    source: str = ""


@dataclass
class Economics:
    """The resolved money model. Build it with :func:`load` or :func:`reload`."""
    prices: List[ModelPrice]
    default_price: Tuple[float, float]
    downtime_profiles: Dict[str, DowntimeProfile]
    active_downtime: str
    billing_model: str
    requests_per_day: int
    cost_max_calls_per_request: float
    nominal_call_cost_usd: float
    per_request_budget_usd: float
    pricing: "OrderedPricing" = field(init=False)

    def __post_init__(self):
        # An ordered {match -> (in, out)} dict plus a "default" key, so older
        # code that read ``config.PRICING`` keeps working unchanged.
        od = OrderedPricing()
        for p in self.prices:
            od[p.match] = p.tuple
        od["default"] = self.default_price
        self.pricing = od

    # -- token cost -----------------------------------------------------------
    def price_for(self, model: Optional[str]) -> Tuple[float, float]:
        """Return (input, output) price per 1e6 tokens for ``model``.

        The first catalog entry whose ``match`` is a substring of the (lowercased)
        model id wins, so a specific id listed before a general one (gpt-4o-mini
        before gpt-4o) is honored. Unknown/empty models fall back to default.
        """
        m = (model or "").lower()
        for p in self.prices:
            if p.match and p.match in m:
                return p.tuple
        return self.default_price

    def cost_usd(self, model: Optional[str], input_tokens: int,
                 output_tokens: int) -> float:
        """Dollar cost of a call at ``model``'s price."""
        p_in, p_out = self.price_for(model)
        return (input_tokens / 1_000_000) * p_in + (output_tokens / 1_000_000) * p_out

    # -- downtime cost --------------------------------------------------------
    def downtime_profile(self, name: Optional[str] = None) -> DowntimeProfile:
        key = name or self.active_downtime
        prof = self.downtime_profiles.get(key)
        if prof is None:
            # Unknown profile name: fall back to the active one, then to a zero
            # profile so callers never crash on a typo.
            prof = self.downtime_profiles.get(self.active_downtime)
        if prof is None:
            prof = DowntimeProfile(name=key, usd_per_minute=0.0,
                                   label="unknown profile", source="")
        return prof

    def downtime_cost_usd(self, minutes: float,
                          profile: Optional[str] = None) -> float:
        """Dollar cost of ``minutes`` of downtime at a named profile's rate."""
        return max(0.0, float(minutes)) * self.downtime_profile(profile).usd_per_minute

    # -- money impact of a heal ----------------------------------------------
    def monthly_spend_usd(self, spend_per_request: float,
                          requests_per_day: Optional[int] = None) -> float:
        rpd = self.requests_per_day if requests_per_day is None else requests_per_day
        return float(spend_per_request) * rpd * 30

    def impact_report(self, *, spend_before: Optional[float] = None,
                      spend_after: Optional[float] = None,
                      mttr_s: Optional[float] = None,
                      requests_per_day: Optional[int] = None,
                      downtime_profile: Optional[str] = None,
                      outage_minutes: float = 10.0) -> dict:
        """Translate a heal into real money, using whatever data is available.

        Returns a dict of numbers plus ready-to-print, dash-free ``lines``. Every
        figure is benchmark-based and configurable, so the report is honest about
        being an estimate, not a measured invoice.
        """
        rpd = self.requests_per_day if requests_per_day is None else requests_per_day
        prof = self.downtime_profile(downtime_profile)
        out: dict = {"lines": [], "requests_per_day": rpd,
                     "downtime_profile": prof.name,
                     "usd_per_minute": prof.usd_per_minute}
        if (spend_before is not None and spend_after is not None
                and spend_before > spend_after):
            per_req_saved = spend_before - spend_after
            monthly = self.monthly_spend_usd(per_req_saved, rpd)
            out["per_request_saved_usd"] = per_req_saved
            out["monthly_spend_saved_usd"] = monthly
            out["lines"].append(
                f"LLM spend: ${per_req_saved:.6f} saved per request, about "
                f"${monthly:,.0f} per month at {rpd:,} requests/day.")
        if prof.usd_per_minute > 0:
            window = self.downtime_cost_usd(outage_minutes, prof.name)
            out["outage_minutes"] = outage_minutes
            out["outage_cost_usd"] = window
            line = (f"Downtime: {prof.label} puts an unattended outage of this class "
                    f"at about ${window:,.0f} for every {outage_minutes:.0f} minutes "
                    f"it stays broken.")
            if mttr_s is not None:
                line += f" The agent cleared it in {mttr_s:.0f}s with 0 humans paged."
            out["lines"].append(line)
        return out


class OrderedPricing(dict):
    """A plain dict that also exposes model families in catalog order.

    ``dict`` already preserves insertion order on modern Python; this subclass
    exists only to make the intent explicit and to give callers a named type.
    """


# --- merge + build -----------------------------------------------------------
def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` onto a copy of ``base``.

    Dicts merge key by key; every other type (including lists) is REPLACED, so a
    user who supplies their own ``pricing.models`` list fully controls it.
    """
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
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _config_path() -> str:
    env = os.getenv("ECONOMICS_CONFIG")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "economics.yaml")


def _f(env_name: str, current):
    """Read a float env override, keeping ``current`` if unset or unparseable."""
    raw = os.getenv(env_name)
    if raw is None or raw == "":
        return current
    try:
        return float(raw)
    except ValueError:
        return current


def _apply_env(raw: dict) -> dict:
    raw = copy.deepcopy(raw)
    raw.setdefault("downtime", {})
    raw.setdefault("slo", {})
    raw.setdefault("budget", {})
    prof = os.getenv("ECON_ACTIVE_DOWNTIME_PROFILE")
    if prof:
        raw["downtime"]["active_profile"] = prof
    model = os.getenv("ECON_BILLING_MODEL")
    if model:
        raw["billing_model"] = model
    rpd = os.getenv("ECON_REQUESTS_PER_DAY")
    if rpd:
        try:
            raw["requests_per_day"] = int(float(rpd))
        except ValueError:
            pass
    raw["slo"]["cost_max_calls_per_request"] = _f(
        "ECON_COST_MAX_CALLS_PER_REQ", raw["slo"].get("cost_max_calls_per_request", 6))
    raw["slo"]["nominal_call_cost_usd"] = _f(
        "ECON_NOMINAL_CALL_COST_USD", raw["slo"].get("nominal_call_cost_usd", 0.00004))
    raw["budget"]["per_request_usd"] = _f(
        "ECON_PER_REQUEST_BUDGET_USD", raw["budget"].get("per_request_usd", 0.0001))
    return raw


def _build(raw: dict) -> Economics:
    pricing = raw.get("pricing", {}) or {}
    dflt = pricing.get("default", {}) or {}
    default_price = (float(dflt.get("input", 0.10)), float(dflt.get("output", 0.10)))
    prices: List[ModelPrice] = []
    for m in pricing.get("models", []) or []:
        try:
            prices.append(ModelPrice(
                match=str(m["match"]).lower(),
                input=float(m["input"]), output=float(m["output"]),
                provider=str(m.get("provider", "")), source=str(m.get("source", ""))))
        except (KeyError, TypeError, ValueError):
            continue

    downtime = raw.get("downtime", {}) or {}
    profiles: Dict[str, DowntimeProfile] = {}
    for name, spec in (downtime.get("profiles", {}) or {}).items():
        try:
            profiles[name] = DowntimeProfile(
                name=name, usd_per_minute=float(spec.get("usd_per_minute", 0) or 0),
                label=str(spec.get("label", "")), source=str(spec.get("source", "")))
        except (TypeError, ValueError):
            continue
    active = downtime.get("active_profile") or "gartner_baseline"

    slo = raw.get("slo", {}) or {}
    budget = raw.get("budget", {}) or {}
    return Economics(
        prices=prices,
        default_price=default_price,
        downtime_profiles=profiles,
        active_downtime=active,
        billing_model=str(raw.get("billing_model", "gpt-4o-mini")),
        requests_per_day=int(raw.get("requests_per_day", 100000) or 100000),
        cost_max_calls_per_request=float(slo.get("cost_max_calls_per_request", 6)),
        nominal_call_cost_usd=float(slo.get("nominal_call_cost_usd", 0.00004)),
        per_request_budget_usd=float(budget.get("per_request_usd", 0.0001)),
    )


def load() -> Economics:
    """Resolve the full money model: defaults, then economics.yaml, then env."""
    raw = _deep_merge(_DEFAULTS, _load_yaml(_config_path()) or {})
    raw = _apply_env(raw)
    return _build(raw)


# --- module-level singleton + convenience API (back-compat surface) ----------
_ECON: Optional[Economics] = None


def _install(econ: Economics) -> Economics:
    global _ECON, PRICING
    _ECON = econ
    PRICING = econ.pricing
    return econ


def _get() -> Economics:
    return _ECON if _ECON is not None else _install(load())


def reload() -> Economics:
    """Rebuild from disk + current env (used by tests after changing env)."""
    return _install(load())


def price_for(model: Optional[str]) -> Tuple[float, float]:
    return _get().price_for(model)


def cost_usd(model: Optional[str], input_tokens: int, output_tokens: int) -> float:
    return _get().cost_usd(model, input_tokens, output_tokens)


def downtime_cost_usd(minutes: float, profile: Optional[str] = None) -> float:
    return _get().downtime_cost_usd(minutes, profile)


def impact_report(**kwargs) -> dict:
    return _get().impact_report(**kwargs)


def default_budget_usd() -> float:
    return _get().per_request_budget_usd


def cost_slo_max_calls_per_request() -> float:
    return _get().cost_max_calls_per_request


def nominal_call_cost_usd() -> float:
    return _get().nominal_call_cost_usd


# Built at import so ``economics.PRICING`` and ``config.PRICING`` are ready.
PRICING: "OrderedPricing" = _get().pricing
