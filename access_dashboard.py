"""Create the 'AccessTrace: WCAG journeys' dashboard (accessibility as telemetry).

This is the Query Builder view of AccessTrace. It reads the accessibility health of
the product from the ``accesstrace`` service that ``access_report.py`` emits: the
``access.journey`` spans carrying the fail-closed WCAG verdict (a11y.status and the
severity-weighted debt) and the ``access.step`` spans that localise WHICH stage of
the user flow breaks, plus the accesstrace.* metrics for the impact-severity mix.

The killer panel is "which stage fails": a keyboard or screen-reader user does not
experience a page as one score, they experience it as a JOURNEY, and the trace shows
exactly where that journey falls apart. Every panel query is re-run through
/api/v5/query_range before the dashboard is created (via dashboard_kit), so the
script proves each panel resolves on the schema it ships with. This builder is a
top-level script: run it once to (re)create the dashboard. Set the dashboard time
range to the last 3 hours to cover a few runs.
"""
import sys

import dashboard_kit as dk

SVC = "service.name = 'accesstrace'"
JOURNEY = f"{SVC} AND name = 'access.journey'"
STEP = f"{SVC} AND name = 'access.step'"

qd_trace = dk.qd_trace
qd_metric = dk.qd_metric
groupby = dk.groupby

# --- panel catalogue: (title, queryData, panelType, yUnit, description, decimals) --
PANELS = [
    # Row 0 -- the verdict at a glance -----------------------------------------
    ("WCAG journeys graded",
     [qd_trace("count()", "journeys", filt=JOURNEY)],
     "value", "short", "TRACES: accessibility journeys driven through the product and graded. "
                       "Each is a real browser walking the flow: land, navigate, read, submit.", 0),
    ("Journeys breaching WCAG",
     [qd_trace("count()", "breaches", filt=f"{JOURNEY} AND a11y.status = 'BREACH'")],
     "value", "short", "TRACES: journeys that blew the WCAG budget (a critical or serious "
                       "violation, or too much weighted debt). Fail closed: a journey that could "
                       "not be exercised is UNKNOWN, never a silent pass.", 0),
    ("Critical violations",
     [qd_trace("sum(a11y.violations.critical)", "critical", filt=JOURNEY)],
     "value", "short", "TRACES: critical WCAG rules broken across the graded journeys (for "
                       "example a control or image with no accessible name). These block a "
                       "screen-reader user outright.", 0),
    ("Serious violations",
     [qd_trace("sum(a11y.violations.serious)", "serious", filt=JOURNEY)],
     "value", "short", "TRACES: serious WCAG rules broken (for example low colour contrast or a "
                       "link with no discernible text). Zero tolerance in the default budget.", 0),
    # Row 1 -- where the debt is and how it splits -----------------------------
    ("Weighted WCAG debt by cohort",
     [qd_trace("avg(a11y.weighted_score)", "{{a11y.cohort}}", filt=JOURNEY,
               gb=groupby("a11y.cohort"))],
     "bar", "none", "TRACES: the severity-weighted violation score per cohort (a critical "
                    "weighs ten minors). The before/after gap between the inaccessible and "
                    "accessible pages is the accessibility win made measurable.", 0),
    ("Violations by impact severity",
     [qd_metric("accesstrace.violation.count", "{{impact}}", gb=groupby("impact"))],
     "bar", "short", "METRICS: accesstrace.violation.count split by axe impact "
                     "(critical / serious / moderate / minor), so the shape of the debt is "
                     "visible, not just its total.", 0),
    ("WCAG verdict mix (PASS / BREACH / UNKNOWN)",
     [qd_trace("count()", "{{a11y.status}}", filt=JOURNEY, gb=groupby("a11y.status"))],
     "pie", "none", "TRACES: journeys split by verdict. UNKNOWN is an honest third state for a "
                    "journey that could not be judged, kept separate from PASS so a blind audit "
                    "never reads as an accessible product.", 0),
    # Row 2 -- the trend and the localisation ----------------------------------
    ("Weighted WCAG debt over time by cohort",
     [qd_trace("avg(a11y.weighted_score)", "{{a11y.cohort}}", filt=JOURNEY,
               gb=groupby("a11y.cohort"))],
     "graph", "none", "TRACES: the weighted WCAG debt per cohort over time. Point this at your "
                      "own site in CI and this line is your accessibility regression trend, "
                      "release over release.", 0),
    ("Breaching journey steps: which stage fails",
     [qd_trace("count()", "{{a11y.step}}", filt=f"{STEP} AND a11y.status = 'BREACH'",
               gb=groupby("a11y.step"))],
     "bar", "short", "TRACES: access.step spans that breached, grouped by journey stage "
                     "(navigation, main content, form). A user experiences the page as a flow, "
                     "and this shows exactly where the flow falls apart.", 0),
]

# 12-column grid: four tiles, then three panels, then two wide panels.
POS = [(0, 0, 3, 4), (3, 0, 3, 4), (6, 0, 3, 4), (9, 0, 3, 4),
       (0, 4, 4, 6), (4, 4, 4, 6), (8, 4, 4, 6),
       (0, 10, 6, 7), (6, 10, 6, 7)]

DESCRIPTION = (
    "AccessTrace turns web accessibility into OpenTelemetry. A real headless browser "
    "walks an accessibility JOURNEY through the product (land, reach the navigation, "
    "reach the main content, reach and fill the form), runs the axe-core WCAG ruleset "
    "at each stage, and grades it with a fail-closed three-state verdict. This board "
    "reads the resulting accesstrace service: the access.journey spans carry the "
    "per-journey verdict (a11y.status and the severity-weighted debt) and the "
    "access.step spans localise which stage of the flow breaks. Set the time range to "
    "the last 3 hours."
)
TAGS = ["accesstrace", "accessibility", "wcag", "a11y", "traces", "metrics",
        "query-builder", "hackathon"]


def main():
    du, ok = dk.ship(
        title="AccessTrace: WCAG journeys (accessibility as telemetry)",
        description=DESCRIPTION, tags=TAGS, panels=PANELS, positions=POS,
        uuid_filename="access_dashboard_uuid.txt",
        export_filename="accesstrace-wcag-journeys.json", window_hours=6)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
