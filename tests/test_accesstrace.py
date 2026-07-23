"""Unit tests for the plug-and-play AccessTrace WCAG audit model.

These lock in the accessibility SLO logic with no browser, no network and no
SigNoz: the violation scoring (counts and severity-weighted score), the fail-closed
three-state verdict (PASS within budget, BREACH on a critical / serious / weighted
breach, UNKNOWN below the minimum journey steps rather than a blind green), the
per-journey de-duplication of a rule seen on many steps, the axe impact folding
(an unranked impact never vanishes from the score), the a11y.* span attribute
flattening, and the layered config resolution (built-in defaults, then access.yaml,
then ACCESS_* environment variables).

Every test that mutates env restores the environment in a finally, so the module
leaves no global state behind.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import access_audit as aa

_ENV_KEYS = ("ACCESS_CONFIG", "ACCESS_MAX_CRITICAL", "ACCESS_MAX_SERIOUS",
             "ACCESS_MAX_WEIGHTED", "ACCESS_MIN_STEPS", "ACCESS_WCAG_TAGS")


def _clear_env():
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


def _v(rule, impact, nodes=1):
    return {"id": rule, "impact": impact, "nodes": [{"n": i} for i in range(nodes)]}


def test_score_counts_and_weighted():
    vios = [_v("a", "critical", 2), _v("b", "serious", 1), _v("c", "minor", 3)]
    s = aa.score(vios, aa.config())
    assert s["counts"] == {"critical": 1, "serious": 1, "moderate": 0, "minor": 1}
    assert s["node_counts"] == {"critical": 2, "serious": 1, "moderate": 0, "minor": 3}
    assert s["total_violations"] == 3
    # default weights: 10 + 5 + 1
    assert s["weighted_score"] == 16.0


def test_verdict_pass_on_clean_journey():
    v = aa.verdict([], steps_completed=4)
    assert v.status == aa.PASS
    assert v.weighted_score == 0.0
    assert v.total_violations == 0
    assert "within WCAG budget" in v.reason


def test_verdict_breach_on_a_single_critical():
    v = aa.verdict([_v("button-name", "critical", 1)], steps_completed=4)
    assert v.status == aa.BREACH
    assert "critical" in v.reason
    assert v.counts["critical"] == 1


def test_verdict_breach_on_a_single_serious():
    v = aa.verdict([_v("color-contrast", "serious", 5)], steps_completed=3)
    assert v.status == aa.BREACH
    assert "serious" in v.reason
    assert v.node_counts["serious"] == 5


def test_verdict_breach_on_weighted_pile_of_moderates():
    # No critical or serious, but many moderates pass the default 0 weighted ceiling.
    vios = [_v(f"m{i}", "moderate", 1) for i in range(3)]
    v = aa.verdict(vios, steps_completed=2)
    assert v.status == aa.BREACH
    assert "weighted" in v.reason
    assert v.weighted_score == 6.0  # 3 moderates * 2.0


def test_verdict_unknown_below_minimum_steps_is_fail_closed():
    # Zero steps completed: even with zero violations it must NOT be a green PASS.
    v = aa.verdict([], steps_completed=0)
    assert v.status == aa.UNKNOWN
    assert "refusing a blind PASS" in v.reason


def test_unranked_impact_folds_into_minor_not_dropped():
    v = aa.verdict([_v("mystery", None, 1), _v("blank", "", 1)], steps_completed=1)
    # both fold to minor (weight 1 each) -> weighted 2 -> breaches default 0 ceiling
    assert v.counts["minor"] == 2
    assert v.weighted_score == 2.0
    assert v.status == aa.BREACH


def test_merge_step_violations_dedupes_by_rule_and_sums_nodes():
    step1 = [_v("color-contrast", "serious", 2), _v("link-name", "moderate", 1)]
    step2 = [_v("color-contrast", "serious", 3)]        # same rule again
    merged = aa.merge_step_violations([step1, step2])
    by_id = {m["id"]: m for m in merged}
    assert set(by_id) == {"color-contrast", "link-name"}
    # nodes summed across the two steps: 2 + 3
    assert len(by_id["color-contrast"]["nodes"]) == 5
    # a rule seen twice is still ONE violated rule in the journey verdict
    v = aa.verdict(merged, steps_completed=2)
    assert v.counts["serious"] == 1


def test_merge_keeps_the_worst_impact_seen_for_a_rule():
    merged = aa.merge_step_violations([
        [_v("aria-x", "moderate", 1)],
        [_v("aria-x", "critical", 1)],   # worse ranking on a later step
    ])
    assert merged[0]["impact"] == "critical"


def test_env_override_raises_serious_tolerance():
    _clear_env()
    try:
        os.environ["ACCESS_MAX_SERIOUS"] = "2"
        os.environ["ACCESS_MAX_WEIGHTED"] = "100"
        cfg = aa.config()
        assert cfg.max_serious == 2
        # 2 serious (weight 10) under both the count and weighted ceilings -> PASS
        v = aa.verdict([_v("a", "serious", 1), _v("b", "serious", 1)],
                       steps_completed=3, cfg=cfg)
        assert v.status == aa.PASS
    finally:
        _clear_env()


def test_env_override_wcag_tags():
    _clear_env()
    try:
        os.environ["ACCESS_WCAG_TAGS"] = "wcag2a, best-practice"
        assert aa.wcag_tags() == ["wcag2a", "best-practice"]
    finally:
        _clear_env()


def test_as_attrs_are_flat_scalars_for_signoz():
    v = aa.verdict([_v("button-name", "critical", 2)], steps_completed=3)
    attrs = v.as_attrs()
    assert attrs["a11y.status"] == aa.BREACH
    assert attrs["a11y.violations.critical"] == 1
    assert attrs["a11y.nodes.critical"] == 2
    assert attrs["a11y.steps"] == 3
    # every value must be a scalar (str / int / float) to be a valid span attribute
    assert all(isinstance(x, (str, int, float)) for x in attrs.values())


def test_default_policy_is_wcag_21_aa():
    _clear_env()
    tags = aa.wcag_tags()
    assert "wcag2aa" in tags and "wcag21aa" in tags


# --- report layer (pure grading and reporting, no browser) -------------------
import access_report as ar


def _journey(cohort, doc_v, stages):
    return {"cohort": cohort, "url": f"file:///{cohort}.html",
            "document_violations": doc_v, "steps_completed": len(stages),
            "stages": stages}


def _stage(step, selector, violations, focusables=2):
    return {"step": step, "selector": selector, "label": f"{step} label",
            "focusables": focusables, "tab_order": ["a:home", "button:go"],
            "violations": violations}


def test_grade_journey_breach_with_per_stage_verdicts():
    doc_v = [_v("image-alt", "critical", 1), _v("label", "critical", 1),
             _v("color-contrast", "serious", 3)]
    stages = [
        _stage("page-load", None, doc_v),
        _stage("navigation", "nav", [_v("color-contrast", "serious", 1)]),
        _stage("main-content", "main", [_v("image-alt", "critical", 1)]),
        _stage("signup-form", "form", [_v("label", "critical", 1)]),
    ]
    g = ar.grade_journey(_journey("inaccessible", doc_v, stages), aa.config())
    assert g["journey_verdict"].status == aa.BREACH
    assert g["steps_completed"] == 4
    by = {s["step"]: s["verdict"].status for s in g["stages"]}
    assert by["navigation"] == aa.BREACH        # serious contrast in the nav
    assert by["main-content"] == aa.BREACH       # missing alt in main


def test_grade_journey_pass_on_clean_page():
    stages = [_stage("page-load", None, []), _stage("navigation", "nav", []),
              _stage("main-content", "main", []), _stage("signup-form", "form", [])]
    g = ar.grade_journey(_journey("accessible", [], stages), aa.config())
    assert g["journey_verdict"].status == aa.PASS
    assert all(s["verdict"].status == aa.PASS for s in g["stages"])


def test_grade_journey_unknown_when_no_stages_ran():
    # A page where no landmark matched: fail closed, never a blind PASS.
    g = ar.grade_journey(_journey("target", [], []), aa.config())
    assert g["journey_verdict"].status == aa.UNKNOWN


def test_build_report_computes_improvement_between_cohorts():
    cfg = aa.config()
    bad = ar.grade_journey(_journey(
        "inaccessible",
        [_v("image-alt", "critical", 1), _v("color-contrast", "serious", 1)],
        [_stage("page-load", None, [_v("image-alt", "critical", 1)])]), cfg)
    good = ar.grade_journey(_journey(
        "accessible", [], [_stage("page-load", None, [])]), cfg)
    report, improvement = ar.build_report([bad, good], cfg, trace_hex="abc123")
    assert report["service"] == "accesstrace"
    assert report["trace"] == "abc123"
    # inaccessible weighted 15 -> accessible 0 = 100 percent reduction
    assert improvement == 100.0
    names = {c["name"]: c for c in report["cohorts"]}
    assert names["inaccessible"]["status"] == aa.BREACH
    assert names["accessible"]["status"] == aa.PASS


def test_worst_status_prefers_breach_then_unknown():
    cfg = aa.config()
    breach = ar.grade_journey(_journey(
        "a", [_v("x", "critical", 1)], [_stage("page-load", None, [])]), cfg)
    unknown = ar.grade_journey(_journey("b", [], []), cfg)
    clean = ar.grade_journey(_journey("c", [], [_stage("page-load", None, [])]), cfg)
    assert ar.worst_status([clean, unknown, breach]) == aa.BREACH
    assert ar.worst_status([clean, unknown]) == aa.UNKNOWN
    assert ar.worst_status([clean]) == aa.PASS


def test_report_cohort_carries_counts_and_stage_rollup():
    cfg = aa.config()
    g = ar.grade_journey(_journey(
        "inaccessible",
        [_v("image-alt", "critical", 2), _v("color-contrast", "serious", 4)],
        [_stage("page-load", None, [_v("image-alt", "critical", 2)]),
         _stage("main-content", "main", [_v("image-alt", "critical", 2)])]), cfg)
    report, _ = ar.build_report([g], cfg)
    c = report["cohorts"][0]
    assert c["counts"]["critical"] == 1 and c["counts"]["serious"] == 1
    assert c["node_counts"]["critical"] == 2 and c["node_counts"]["serious"] == 4
    steps = {s["step"]: s for s in c["stages"]}
    assert steps["main-content"]["status"] == aa.BREACH
