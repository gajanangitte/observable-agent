# AccessTrace: web accessibility as OpenTelemetry

Everyone in this hackathon observes machines: latency, tokens, errors, energy.
AccessTrace observes the one thing that decides whether a human can actually use the
product at all: **web accessibility**. It drives a real browser through an
accessibility **journey**, runs the axe-core WCAG ruleset at each stage, and turns
the result into a graded, alertable, trace backed verdict in your self hosted SigNoz.

A page is not "accessible" because it loads fast or returns a 200. It is accessible
only when a person on a keyboard or a screen reader can complete the flow: land,
find the navigation, read the content, fill the form, submit. That is a journey, and
a journey is a trace. AccessTrace makes the accessibility of that journey a first
class, observable SLO that regresses loudly the moment a release breaks it.

| | |
|---|---|
| Service in SigNoz | `accesstrace` |
| North star | critical plus serious WCAG violations per **judged** journey (fail closed: UNKNOWN, never a blind pass) |
| Dashboard | AccessTrace: WCAG journeys (accessibility as telemetry) |
| Dashboard UUID (this instance) | `019f8db3-3ea2-7cf8-9116-a496be02f9f3` |
| Importable JSON | [`dashboards/accesstrace-wcag-journeys.json`](../dashboards/accesstrace-wcag-journeys.json) |
| Alerts | AccessTrace WCAG Budget Breach (critical), AccessTrace Breaching Stage (warning) |
| Runner | [`access_report.py`](../access_report.py) |
| Plug and play policy | [`access_audit.py`](../access_audit.py), [`access.yaml`](../access.yaml) |
| Shared dashboard core | [`dashboard_kit.py`](../dashboard_kit.py) (ProofKit) |
| Tests | [`tests/test_accesstrace.py`](../tests/test_accesstrace.py) (19), [`tests/test_dashboard_kit.py`](../tests/test_dashboard_kit.py) (9), [`tests/test_access_alert.py`](../tests/test_access_alert.py) (6), all network free |

## Why this is different

The self healing agent proves the closed loop for Track 01, the MCP Contract Lab
certifies the protocol for Track 02, and WattTrace measures energy for Track 03.
AccessTrace takes the exact same primitives, record raw facts, grade them with a
deterministic fail closed three state verdict, alert on SigNoz, and points them at a
signal that is usually trapped in a one off CI report that nobody watches: whether
the product is usable by a disabled person, and whether that is getting better or
worse per release.

It reframes accessibility as observability. axe-core already exists and is excellent,
but teams run it once, read a wall of JSON, fix a few things, and forget. AccessTrace
makes the WCAG verdict a **live telemetry stream**: a trace you can drill, a
dashboard you can trend, and an alert that pages you when a deploy regresses the
journey. The same SigNoz you already run for latency now watches your users who
navigate by keyboard.

## An accessibility journey is a distributed trace

A sighted mouse user experiences a page as one glance. A keyboard or screen reader
user experiences it as a **sequence**: they land, they tab into the navigation, they
move to the main content, they reach the form, they submit. AccessTrace walks that
same sequence in a real headless Chromium and records it as a trace:

```
access.suite                         (the whole run, service accesstrace)
  access.journey  cohort=inaccessible  a11y.status=BREACH  weighted=40
    access.step  page-load     BREACH  (2 critical, 4 serious)
    access.step  navigation    BREACH  (icon link with no name, low contrast)
    access.step  main-content  BREACH  (image with no alt text)
    access.step  signup-form   BREACH  (input with no label, unnamed submit)
  access.journey  cohort=accessible    a11y.status=PASS    weighted=0
    access.step  page-load     PASS
    access.step  navigation    PASS
    access.step  main-content  PASS
    access.step  signup-form   PASS
```

Every `access.step` span carries the axe violations found at that stage as span
events, the keyboard focus order it observed, and its own three state verdict. So the
trace does not just say "the page scores badly", it shows you **exactly which stage
of the user flow falls apart**, which is the first question a developer asks.

## The verdict is fail closed

The grade is a three state verdict, the same shape the self healer and WattTrace use:

* **PASS**: the journey stayed within the WCAG budget (no critical, no serious, and
  under the weighted debt ceiling).
* **BREACH**: it broke a critical or serious rule, or piled up too much weighted debt.
* **UNKNOWN**: the journey could not be exercised far enough to judge (no landmark
  matched, or axe could not run). This is the important one. A blind audit is
  **never** reported as an accessible product. It is UNKNOWN, kept separate from
  PASS, so a broken probe can never quietly turn the board green.

The score is severity weighted, not a raw rule count. A critical violation (a control
or image with no accessible name) weighs ten times a minor one, so the budget gates on
the issues that actually block a user, not on best practice noise that swings between
releases. The default budget is zero tolerance for critical and serious, which is the
bar most accessibility law leans on (ADA, EN 301 549, Section 508, all built on WCAG
2.1 AA).

## Plug and play policy

Everything about what counts as a breach lives in one place a team can own, exactly
like the WattTrace energy model and the economics layer. [`access.yaml`](../access.yaml)
sets the WCAG rule tags to enforce, the severity weights, and the budget. Precedence
is built in defaults, then `access.yaml`, then `ACCESS_*` environment variables, so a
fresh clone and the test suite work with no config, and a per environment override is
one variable:

```bash
# Carry a known backlog of minor issues while still gating new critical/serious ones
ACCESS_MAX_WEIGHTED=20 python access_report.py --cohort accessible --gate

# Audit any live page you own, no code changes
python access_report.py --url https://your-product.example/
```

Point `--url` at your real site and the same generic landmark journey (navigation,
main content, form) runs against it. If a landmark is missing the stage is skipped,
and if too little of the journey can be exercised the verdict fails closed to UNKNOWN.

## Live proof

Two cohorts, a broken page and its fixed twin, driven through the identical journey:

* **inaccessible**: verdict **BREACH**, weighted debt **40** (2 critical, 4 serious).
  The trace localises each failure to its stage: an icon only navigation link with no
  accessible name and low contrast text, a hero image with no `alt`, a form input with
  no label, and a submit button with no accessible name.
* **accessible**: verdict **PASS**, weighted debt **0**, every stage clean.
* **Improvement**: fixing the page cut the weighted WCAG debt by **100 percent**
  across the same journey.

The run emits all of it to SigNoz on the `accesstrace` service (verified live: the
`access.suite` trace and its journey and step spans land and are queryable through
`/api/v5/query_range`). The dashboard self verifies all nine panels against live data
before it is created, and the two alerts fire on a real BREACH.

## The CI accessibility gate

Because the verdict is fail closed, the same run doubles as a CI gate:

```bash
python access_report.py --cohort accessible --gate   # exit 1 on BREACH, 2 on UNKNOWN
```

A pull request that regresses accessibility fails the build on the very verdict SigNoz
alerts on, so the dashboard, the alert, and the gate all read the one source of truth.

## What SigNoz does here

* **Traces**: the `access.suite` root, an `access.journey` span per cohort carrying
  the WCAG verdict, and an `access.step` span per stage with its own verdict, focus
  order, and a `wcag.violation` event per broken rule. Drill the trace to see where a
  journey breaks.
* **Metrics**: `accesstrace.violation.count` and `accesstrace.node.count` by impact and
  cohort, `accesstrace.journey.count` by status, `accesstrace.weighted_score`, and
  `accesstrace.focusable.count`.
* **Dashboard**: nine panels, self verified, including the killer "which stage fails"
  breakdown and a weighted debt trend you can point at your own site for a release over
  release accessibility regression line.
* **Alerts**: a critical page when a journey breaches, and a warning that localises the
  breach to a stage.

## Reproduce it

```bash
pip install -r requirements.txt
playwright install chromium              # one time, downloads the browser
python access_report.py                  # inaccessible vs accessible demo, exports to SigNoz
python access_dashboard.py               # (re)create the self verifying dashboard
python access_alert.py --ensure          # create the two alerts
python tests/run_all.py                  # 210 network free tests, includes AccessTrace
```

The demo pages ([`access_demo/inaccessible.html`](../access_demo/inaccessible.html)
and [`access_demo/accessible.html`](../access_demo/accessible.html)) and the vendored
axe-core (`access_demo/axe.min.js`, MPL 2.0) are self contained, so the whole loop runs
offline with no network beyond the local SigNoz.

## Honest notes

* axe-core catches a large, high value slice of WCAG but not all of it. Some criteria
  (meaningful alt text quality, logical reading order in every case) still need a human.
  AccessTrace makes the machine checkable part continuous and observable, it does not
  claim to replace an audit.
* Color contrast checks need a real rendered browser, which is why AccessTrace drives
  headless Chromium rather than parsing static HTML.
* The energy of running the browser is not counted against the product. AccessTrace
  measures the product, not itself.
