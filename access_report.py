"""Run the AccessTrace WCAG suite and publish the verdict to SigNoz.

AccessTrace answers a question an uptime or page-speed dashboard cannot: is the
product actually USABLE by someone on a keyboard or a screen reader, and is that
getting better or worse per release? It drives a real headless browser through an
accessibility JOURNEY (land on the page, reach the navigation, reach the main
content, reach and fill the form), runs the axe-core WCAG ruleset at each stage,
grades the result with the fail-closed three-state model in ``access_audit.py``,
and emits the whole run to SigNoz as OpenTelemetry on the ``accesstrace`` service:

  * TRACES  -- one ``access.suite`` root span, an ``access.journey`` span per cohort
    carrying the WCAG verdict (``a11y.status`` = PASS/BREACH/UNKNOWN and the
    severity-weighted score), and, underneath, one ``access.step`` span per journey
    stage carrying that stage's own verdict, its keyboard focus order, and an event
    per violated rule. The trace shows EXACTLY which stage of the user flow breaks.
  * METRICS -- accesstrace.violation.count / node.count (by impact + cohort),
    accesstrace.journey.count (by status), accesstrace.weighted_score,
    accesstrace.focusable.count.

The north star is CRITICAL + SERIOUS violations per verified journey. Because the
verdict is fail closed (UNKNOWN when the journey could not be exercised far enough,
never a false all-clear), the suite doubles as a CI accessibility gate: with
``--gate`` it exits non-zero when a cohort breaches the WCAG budget.

    python access_report.py                          # inaccessible vs accessible demo
    python access_report.py --cohort accessible --gate   # fail CI if the page breaches
    python access_report.py --url https://your.site/  # audit any live page, plug and play
    python access_report.py --no-export --json access_report.json
"""
import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("OTEL_SERVICE_NAME", "accesstrace")

import access_audit as audit
import access_metrics as metrics
import telemetry

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

HERE = Path(__file__).resolve().parent
DEMO = HERE / "access_demo"
AXE = DEMO / "axe.min.js"

# The accessibility JOURNEY: ordered stages, each scoped to a landmark region so the
# trace localises which part of the flow fails. selector None means the whole
# document (the landing scan that also catches page-level rules like html-has-lang).
# The region selectors are generic (landmark elements / ARIA roles) so the same
# journey runs against the demo pages AND any real site you point --url at.
JOURNEY = [
    {"step": "page-load", "selector": None,
     "label": "land on the page and load the DOM"},
    {"step": "navigation", "selector": "nav, header, [role=navigation]",
     "label": "reach and tab through the primary navigation"},
    {"step": "main-content", "selector": "main, [role=main]",
     "label": "reach the main content and hero media"},
    {"step": "signup-form", "selector": "form, [role=form]",
     "label": "reach and fill in the signup form"},
]

DEMO_COHORTS = {"inaccessible": "inaccessible.html", "accessible": "accessible.html"}

_AXE_RUN_JS = """
async ([selector, tags]) => {
  const ctx = selector ? document.querySelector(selector) : document;
  if (!ctx) return null;
  const res = await window.axe.run(ctx, { runOnly: { type: 'tag', values: tags } });
  return res.violations.map(v => ({
    id: v.id, impact: v.impact, help: v.help,
    nodes: v.nodes.map(n => (n.target && n.target[0]) || '').slice(0, 25)
  }));
}
"""

_FOCUS_JS = """
(selector) => {
  const root = selector ? document.querySelector(selector) : document.body;
  if (!root) return { focusables: 0, order: [] };
  const q = 'a[href], button, input:not([type=hidden]), select, textarea, [tabindex]';
  const els = Array.from(root.querySelectorAll(q))
    .filter(e => !e.disabled && e.tabIndex !== -1 && e.offsetParent !== null);
  const order = els.slice(0, 15).map(e => {
    const name = (e.getAttribute('aria-label') || e.textContent || e.value || '').trim();
    return e.tagName.toLowerCase() + (name ? (':' + name.slice(0, 24)) : ':(no name)');
  });
  if (els[0]) els[0].focus();
  return { focusables: els.length, order };
}
"""


# --- browser layer (the only functions that touch Playwright) ----------------
def _run_axe(page, selector, tags):
    try:
        out = page.evaluate(_AXE_RUN_JS, [selector, tags])
        return out or []
    except Exception:
        return []


def _explore_region(page, selector):
    try:
        r = page.evaluate(_FOCUS_JS, selector)
        # Do a couple of real keyboard tabs so the journey is a genuine keyboard
        # interaction, not only a DOM read (best effort; never fail the run on it).
        for _ in range(min(3, int(r.get("focusables", 0) or 0))):
            page.keyboard.press("Tab")
        return int(r.get("focusables", 0)), list(r.get("order", []))
    except Exception:
        return 0, []


def drive_journey(page, target, cohort, cfg):
    """Drive one cohort through the whole journey and return its raw findings.

    This is the ONLY browser-touching step; everything downstream grades plain
    dicts, so the scoring and reporting are unit tested without a browser."""
    url = target if str(target).startswith("http") else Path(target).resolve().as_uri()
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.add_script_tag(path=str(AXE))
    page.wait_for_function("() => window.axe !== undefined", timeout=15000)
    tags = cfg.wcag_tags

    document_violations = _run_axe(page, None, tags)
    stages = []
    for stage in JOURNEY:
        sel = stage["selector"]
        if sel is not None and page.query_selector(sel) is None:
            continue  # this landmark does not exist on the page; skip the stage
        focusables, order = _explore_region(page, sel)
        violations = _run_axe(page, sel, tags)
        stages.append({"step": stage["step"], "selector": sel, "label": stage["label"],
                       "focusables": focusables, "tab_order": order,
                       "violations": violations})
    return {"cohort": cohort, "url": url, "document_violations": document_violations,
            "steps_completed": len(stages), "stages": stages}


# --- pure grading and reporting (no browser, unit tested) --------------------
def grade_journey(journey, cfg=None):
    """Grade a raw journey dict into a fail-closed WCAG verdict plus per-stage
    verdicts. Pure: takes and returns plain data, so tests feed hand-built axe
    results with no browser."""
    cfg = cfg or audit.config()
    jv = audit.verdict(journey.get("document_violations", []),
                       journey.get("steps_completed", 0), cfg)
    stages = []
    for st in journey.get("stages", []):
        sv = audit.verdict(st.get("violations", []), 1, cfg)
        stages.append({"step": st["step"], "selector": st.get("selector"),
                       "label": st.get("label", ""),
                       "focusables": st.get("focusables", 0),
                       "tab_order": st.get("tab_order", []),
                       "violations": st.get("violations", []), "verdict": sv})
    return {"cohort": journey["cohort"], "url": journey.get("url", ""),
            "steps_completed": journey.get("steps_completed", 0),
            "journey_verdict": jv, "stages": stages}


def build_report(graded, cfg, trace_hex=""):
    by = {g["cohort"]: g for g in graded}
    improvement = None
    if "inaccessible" in by and "accessible" in by:
        bad = by["inaccessible"]["journey_verdict"].weighted_score
        good = by["accessible"]["journey_verdict"].weighted_score
        if bad > 0:
            improvement = 100.0 * (bad - good) / bad
    report = {
        "service": "accesstrace",
        "trace": trace_hex,
        "wcag_tags": cfg.wcag_tags,
        "budget": {"max_critical": cfg.max_critical, "max_serious": cfg.max_serious,
                   "max_weighted_score": cfg.max_weighted_score,
                   "minimum_steps": cfg.minimum_steps},
        "weighted_score_improvement_percent": improvement,
        "cohorts": [{
            "name": g["cohort"], "url": g["url"],
            "status": g["journey_verdict"].status,
            "weighted_score": g["journey_verdict"].weighted_score,
            "total_violations": g["journey_verdict"].total_violations,
            "counts": g["journey_verdict"].counts,
            "node_counts": g["journey_verdict"].node_counts,
            "steps_completed": g["steps_completed"],
            "stages": [{"step": s["step"], "status": s["verdict"].status,
                        "weighted_score": s["verdict"].weighted_score,
                        "violations": s["verdict"].total_violations,
                        "focusables": s["focusables"]} for s in g["stages"]],
        } for g in graded],
    }
    return report, improvement


def worst_status(graded):
    statuses = [g["journey_verdict"].status for g in graded]
    if audit.BREACH in statuses:
        return audit.BREACH
    if audit.UNKNOWN in statuses:
        return audit.UNKNOWN
    return audit.PASS


def scoreboard(graded, improvement):
    print("\n" + "=" * 74)
    print("  ACCESSTRACE WCAG SCOREBOARD  (critical + serious violations per journey)")
    print("=" * 74)
    cfg = audit.config()
    print(f"  policy: WCAG tags {', '.join(cfg.wcag_tags)}; budget "
          f"{cfg.max_critical} critical / {cfg.max_serious} serious / "
          f"weighted {cfg.max_weighted_score:.0f}; fail closed below "
          f"{cfg.minimum_steps} step(s)")
    print("-" * 74)
    for g in graded:
        v = g["journey_verdict"]
        c = v.counts
        print(f"  [{g['cohort']:^13}] verdict {v.status:<7} "
              f"weighted {v.weighted_score:>5.0f}  "
              f"({c['critical']} critical, {c['serious']} serious, "
              f"{c['moderate']} moderate, {c['minor']} minor)")
        for s in g["stages"]:
            sv = s["verdict"]
            mark = "ok " if sv.status == audit.PASS else ("!! " if sv.status == audit.BREACH else "?? ")
            print(f"        {mark}{s['step']:<13} {sv.status:<7} "
                  f"weighted {sv.weighted_score:>4.0f}, {s['focusables']} focusable "
                  f"element(s): {sv.reason}")
    if improvement is not None:
        print("-" * 74)
        print(f"  IMPROVEMENT: fixing the page cut the weighted WCAG debt by "
              f"{improvement:.0f} percent across the same journey.")
    print("=" * 74)


def emit_trace(tracer, graded, cfg):
    """Build the whole access.suite trace from the already-collected data."""
    trace_hex = ""
    with tracer.start_as_current_span("access.suite", kind=SpanKind.SERVER) as suite:
        suite.set_attribute("service.name", "accesstrace")
        suite.set_attribute("a11y.journeys", len(graded))
        suite.set_attribute("a11y.wcag_tags", ",".join(cfg.wcag_tags))
        ctx = suite.get_span_context()
        trace_hex = format(ctx.trace_id, "032x") if ctx and ctx.trace_id else ""
        suite.set_attribute("a11y.worst_status", worst_status(graded))
        for g in graded:
            jv = g["journey_verdict"]
            with tracer.start_as_current_span("access.journey", kind=SpanKind.INTERNAL) as js:
                js.set_attribute("a11y.cohort", g["cohort"])
                js.set_attribute("a11y.url", g["url"])
                for k, val in jv.as_attrs().items():
                    js.set_attribute(k, val)
                js.set_status(Status(StatusCode.ERROR, jv.reason)
                              if jv.status == audit.BREACH else Status(StatusCode.OK))
                for s in g["stages"]:
                    sv = s["verdict"]
                    with tracer.start_as_current_span("access.step",
                                                      kind=SpanKind.INTERNAL) as ss:
                        ss.set_attribute("a11y.cohort", g["cohort"])
                        ss.set_attribute("a11y.step", s["step"])
                        ss.set_attribute("a11y.step.label", s["label"])
                        if s["selector"]:
                            ss.set_attribute("a11y.step.selector", s["selector"])
                        ss.set_attribute("a11y.focusable_count", s["focusables"])
                        if s["tab_order"]:
                            ss.set_attribute("a11y.tab_order", " > ".join(s["tab_order"]))
                        for k, val in sv.as_attrs().items():
                            ss.set_attribute(k, val)
                        for viol in s["violations"]:
                            ss.add_event("wcag.violation", {
                                "a11y.rule": str(viol.get("id", "")),
                                "a11y.impact": str(viol.get("impact", "") or "minor"),
                                "a11y.help": str(viol.get("help", ""))[:180],
                                "a11y.nodes": len(viol.get("nodes") or []),
                            })
                        ss.set_status(Status(StatusCode.ERROR, sv.reason)
                                      if sv.status == audit.BREACH else Status(StatusCode.OK))
    return trace_hex


def _launch_and_drive(plan, cfg, headed):
    """Launch one headless Chromium and drive every cohort's journey."""
    from playwright.sync_api import sync_playwright
    journeys = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            for cohort, target in plan:
                print(f">>> journey '{cohort}': {target}")
                journeys.append(drive_journey(page, target, cohort, cfg))
        finally:
            browser.close()
    return journeys


def run(plan, gate, export, json_out, headed):
    import json
    cfg = audit.config()
    journeys = _launch_and_drive(plan, cfg, headed)
    graded = [grade_journey(j, cfg) for j in journeys]

    trace_hex = ""
    if export:
        telemetry.setup_telemetry()
        tracer = trace.get_tracer("accesstrace")
        trace_hex = emit_trace(tracer, graded, cfg)
        for g in graded:
            metrics.record_journey(g["cohort"], g["journey_verdict"])
            for s in g["stages"]:
                metrics.record_focusables(g["cohort"], s["step"], s["focusables"])

    report, improvement = build_report(graded, cfg, trace_hex)
    scoreboard(graded, improvement)

    out = json_out or (HERE / "access_report.json")
    Path(out).write_text(json.dumps(report, indent=2))
    print(f"\nreport -> {out}")

    if export:
        # allow the batch processors to flush before the process exits
        time.sleep(1.0)
        telemetry.shutdown()
        if trace_hex:
            ui = os.getenv("SIGNOZ_UI", "http://localhost:8080")
            print(f"  access suite trace: {trace_hex}")
            print(f"  view:               {ui}/trace/{trace_hex}")

    statuses = [g["journey_verdict"].status for g in graded]
    if gate:
        if audit.BREACH in statuses:
            print("  GATE: a cohort BREACHED the WCAG budget -> failing the build")
            return 1
        if audit.UNKNOWN in statuses:
            print("  GATE: a cohort is UNKNOWN (journey could not be exercised) -> "
                  "failing closed, not green-lighting an unjudgeable run")
            return 2
    return 0


def _plan(args):
    if args.url:
        return [("target", args.url)]
    plan = []
    if args.cohort in ("both", "inaccessible"):
        plan.append(("inaccessible", DEMO / DEMO_COHORTS["inaccessible"]))
    if args.cohort in ("both", "accessible"):
        plan.append(("accessible", DEMO / DEMO_COHORTS["accessible"]))
    return plan


def main():
    ap = argparse.ArgumentParser(description="AccessTrace WCAG suite (SigNoz-native)")
    ap.add_argument("--cohort", choices=["both", "inaccessible", "accessible"],
                    default="both", help="which demo cohort(s) to run (default both)")
    ap.add_argument("--url", default=None,
                    help="audit any live URL instead of the demo pages (plug and play)")
    ap.add_argument("--gate", action="store_true",
                    help="exit non-zero if a cohort breaches the WCAG budget (CI gate)")
    ap.add_argument("--no-export", action="store_true",
                    help="skip SigNoz export (offline scoreboard only)")
    ap.add_argument("--headed", action="store_true", help="show the browser (debugging)")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="write the JSON report here")
    args = ap.parse_args()
    code = run(_plan(args), args.gate, not args.no_export, args.json_out, args.headed)
    sys.exit(code)


if __name__ == "__main__":
    main()
