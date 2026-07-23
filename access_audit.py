"""Plug-and-play accessibility (WCAG) audit model for AccessTrace.

AccessTrace answers a question a page-speed or uptime dashboard cannot: is the
product actually USABLE by someone on a keyboard or a screen reader, and is that
getting better or worse per release? It drives a real browser through an
accessibility JOURNEY (land, tab, activate, reach a goal), runs the axe-core WCAG
ruleset at each step, and grades the result with the SAME fail-closed three-state
logic the self-healer and WattTrace use. This file is the MODEL: it turns a set of
axe-core violations into a weighted score and a PASS / BREACH / UNKNOWN verdict.

Everything about WHAT counts as a breach lives here (and in ``access.yaml``): which
WCAG rule tags to enforce, how much each severity weighs, and the budget the alert
and the CI gate hold the product to. A team plugs in its OWN policy by editing
``access.yaml`` or setting a few ``ACCESS_*`` environment variables. No code
changes, no redeploy. Plug and play.

Resolution order, lowest to highest precedence (identical shape to energy.py and
economics.py):

  1. the built-in defaults in this file (WCAG 2.1 AA, zero-tolerance for the two
     serious severities), so a fresh clone and the test suite just work.
  2. ``access.yaml`` (path via the ``ACCESS_CONFIG`` env var, default the repo
     file), the single file you edit to make the policy yours.
  3. individual ``ACCESS_*`` environment variables, for quick per-run overrides.

HONESTY: the verdict is FAIL CLOSED. A journey that could not be driven far enough
to judge (fewer than the minimum steps, or axe could not run) is UNKNOWN, never a
green PASS. A blind audit must never read as an accessible product.

Design notes: NO network calls, fully deterministic, does NOT import ``config``,
``telemetry`` or ``agent`` (so any of them may import this without a cycle). YAML is
optional: if PyYAML is missing or the file is absent, the built-in defaults are
used, so nothing ever hard-fails.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:  # dotenv is already a dependency; load defensively for direct imports.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is always present in this project
    pass

# Three-state verdict, fail closed, exactly like energy.py and heal_sensors.py.
PASS = "PASS"
BREACH = "BREACH"
UNKNOWN = "UNKNOWN"

# The four axe-core impact levels, worst to least. "none"/unknown impacts fold into
# minor so a rule that axe could not rank is never silently dropped from the score.
IMPACTS = ("critical", "serious", "moderate", "minor")


# --- Built-in defaults (mirror access.yaml; the offline-safe source) ----------
_DEFAULTS: dict = {
    # Which axe-core rule tags to run. WCAG 2.1 AA is the common legal bar (ADA /
    # EN 301 549 / Section 508 all lean on it), plus best-practice rules axe ships.
    "wcag_tags": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"],
    # How much a single violated rule of each severity weighs in the journey score.
    # A critical (e.g. a control with no accessible name) is worth ten minors.
    "severity_weights": {"critical": 10.0, "serious": 5.0, "moderate": 2.0, "minor": 1.0},
    "budget": {
        # Zero tolerance for the two severities that actually block a user: any
        # critical or serious violation breaches. Moderates and minors are absorbed
        # by the weighted ceiling so a pile of small issues still trips the SLO.
        "max_critical": 0,
        "max_serious": 0,
        # Weighted-score ceiling PER journey (sum of weight times violated-rule
        # count). 0 means "any weighted violation breaches"; raise it to allow a
        # known backlog of minors while still gating regressions.
        "max_weighted_score": 0.0,
        # Fail closed below this many completed journey steps: too little was
        # exercised to call the product accessible.
        "minimum_steps": 1,
    },
}

_TRUE = {"1", "true", "yes", "on"}


# --- typed model -------------------------------------------------------------
@dataclass(frozen=True)
class AuditConfig:
    wcag_tags: List[str]
    severity_weights: Dict[str, float]
    max_critical: int
    max_serious: int
    max_weighted_score: float
    minimum_steps: int


@dataclass(frozen=True)
class Verdict:
    status: str
    weighted_score: float
    counts: Dict[str, int]              # violated-rule count per impact
    node_counts: Dict[str, int]         # offending element count per impact
    total_violations: int               # distinct violated rules
    steps: int
    reason: str

    def as_attrs(self) -> Dict[str, object]:
        """Flatten to a11y.* span attributes (all scalars, SigNoz friendly)."""
        a: Dict[str, object] = {
            "a11y.status": self.status,
            "a11y.weighted_score": round(self.weighted_score, 2),
            "a11y.violations": self.total_violations,
            "a11y.steps": self.steps,
            "a11y.reason": self.reason,
        }
        for imp in IMPACTS:
            a[f"a11y.violations.{imp}"] = self.counts.get(imp, 0)
            a[f"a11y.nodes.{imp}"] = self.node_counts.get(imp, 0)
        return a


def _yaml_config() -> dict:
    path = os.getenv("ACCESS_CONFIG", os.path.join(os.path.dirname(__file__), "access.yaml"))
    if not path or not os.path.exists(path):
        return {}
    try:
        import yaml  # optional; defaults cover a box without PyYAML
        with open(path, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _env_num(name, default, cast):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def _resolve() -> AuditConfig:
    cfg = _merge(_DEFAULTS, _yaml_config())
    b = cfg.get("budget", {})
    weights = {k: float(v) for k, v in (cfg.get("severity_weights") or {}).items()}
    for imp in IMPACTS:
        weights.setdefault(imp, _DEFAULTS["severity_weights"][imp])

    tags = cfg.get("wcag_tags") or list(_DEFAULTS["wcag_tags"])
    env_tags = os.getenv("ACCESS_WCAG_TAGS")
    if env_tags:
        tags = [t.strip() for t in env_tags.split(",") if t.strip()]

    return AuditConfig(
        wcag_tags=list(tags),
        severity_weights=weights,
        max_critical=_env_num("ACCESS_MAX_CRITICAL", int(b.get("max_critical", 0)), int),
        max_serious=_env_num("ACCESS_MAX_SERIOUS", int(b.get("max_serious", 0)), int),
        max_weighted_score=_env_num("ACCESS_MAX_WEIGHTED",
                                    float(b.get("max_weighted_score", 0.0)), float),
        minimum_steps=_env_num("ACCESS_MIN_STEPS", int(b.get("minimum_steps", 1)), int),
    )


def config() -> AuditConfig:
    """Resolve the live audit policy (defaults <- access.yaml <- ACCESS_* env)."""
    return _resolve()


def wcag_tags() -> List[str]:
    """The axe-core rule tags the runner should enable for this policy."""
    return _resolve().wcag_tags


def _impact_of(v: dict) -> str:
    imp = (v.get("impact") or "").lower()
    return imp if imp in IMPACTS else "minor"


def score(violations: List[dict], cfg: Optional[AuditConfig] = None) -> dict:
    """Reduce a list of axe-core violation objects to counts and a weighted score.

    Each violation is one VIOLATED RULE; ``nodes`` is the list of offending
    elements for that rule. We count both: rules drive the pass/fail budget (a rule
    is broken or it is not), nodes size the blast radius for the dashboard."""
    cfg = cfg or _resolve()
    counts = {imp: 0 for imp in IMPACTS}
    node_counts = {imp: 0 for imp in IMPACTS}
    weighted = 0.0
    for v in violations or []:
        imp = _impact_of(v)
        counts[imp] += 1
        nodes = v.get("nodes")
        node_counts[imp] += len(nodes) if isinstance(nodes, list) else int(nodes or 0)
        weighted += cfg.severity_weights.get(imp, 1.0)
    return {
        "counts": counts,
        "node_counts": node_counts,
        "weighted_score": weighted,
        "total_violations": sum(counts.values()),
    }


def verdict(violations: List[dict], steps_completed: int,
            cfg: Optional[AuditConfig] = None) -> Verdict:
    """Fail-closed WCAG SLO for one accessibility journey.

    UNKNOWN (never a green PASS) when the journey could not be exercised far enough
    to judge. Otherwise BREACH if it broke the critical / serious ceiling or the
    weighted-score budget, else PASS. The severity budget is what makes this robust:
    it gates on the issues that actually block a user, not on raw rule counts that
    swing with unrelated best-practice noise."""
    cfg = cfg or _resolve()
    s = score(violations, cfg)
    counts, weighted = s["counts"], s["weighted_score"]
    if steps_completed < cfg.minimum_steps:
        return Verdict(UNKNOWN, weighted, counts, s["node_counts"], s["total_violations"],
                       steps_completed,
                       reason=f"only {steps_completed} journey step(s) completed "
                              f"(need {cfg.minimum_steps} to judge); refusing a blind PASS")
    over_crit = counts["critical"] > cfg.max_critical
    over_serious = counts["serious"] > cfg.max_serious
    over_weighted = weighted > cfg.max_weighted_score
    if over_crit or over_serious or over_weighted:
        parts = []
        if over_crit:
            parts.append(f"{counts['critical']} critical (budget {cfg.max_critical})")
        if over_serious:
            parts.append(f"{counts['serious']} serious (budget {cfg.max_serious})")
        if over_weighted:
            parts.append(f"weighted {weighted:.0f} (budget {cfg.max_weighted_score:.0f})")
        return Verdict(BREACH, weighted, counts, s["node_counts"], s["total_violations"],
                       steps_completed, reason="WCAG budget exceeded: " + ", ".join(parts))
    return Verdict(PASS, weighted, counts, s["node_counts"], s["total_violations"],
                   steps_completed,
                   reason=f"within WCAG budget across {steps_completed} step(s) "
                          f"(0 critical, 0 serious, weighted {weighted:.0f})")


def merge_step_violations(steps: List[List[dict]]) -> List[dict]:
    """Fold per-step violation lists into ONE journey-level list, de-duplicated by
    axe rule id (a rule broken on three steps is one broken rule, its nodes summed),
    so the journey verdict is not inflated by re-seeing the same issue each step."""
    by_id: Dict[str, dict] = {}
    for step in steps or []:
        for v in step or []:
            rid = v.get("id") or v.get("help") or repr(v)
            if rid not in by_id:
                by_id[rid] = {"id": rid, "impact": v.get("impact"),
                              "nodes": list(v.get("nodes") or [])}
            else:
                cur = by_id[rid]
                cur["nodes"] = list(cur.get("nodes") or []) + list(v.get("nodes") or [])
                # keep the worst impact seen for this rule
                if IMPACTS.index(_impact_of(v)) < IMPACTS.index(_impact_of(cur)):
                    cur["impact"] = v.get("impact")
    return list(by_id.values())
