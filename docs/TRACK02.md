# Track 02: Signals and Dashboards

Track 02 rewards two things: honest OpenTelemetry instrumentation, and Query Builder
mastery. This project earns both from a single artifact, the dashboard **"Agent
Reliability: Three Signals"**, which reads the self healing loop from all three OTel
signals at once.

| | |
|---|---|
| Dashboard | Agent Reliability: Three Signals (traces + metrics + logs) |
| UUID (this instance) | `019f7e12-4e3e-7ddd-bec6-c18d9e4c15f6` |
| Importable JSON | [`dashboards/agent-reliability-three-signals.json`](../dashboards/agent-reliability-three-signals.json) |
| Builder | [`dashboard_fullsignal.py`](../dashboard_fullsignal.py) |

## Why three signals

Most agent dashboards show one signal. The self healer emits all three, so the same
incident can be read three ways and cross checked:

* **Traces** answer *what the control loop did*: one `agent.heal` root span per cycle,
  with the detect, decide, canary, verify children underneath.
* **Metrics** answer *how often and how well*: purpose built counters for SLO breaches,
  heal outcomes, the decision source, and the must stay zero unsafe counter.
* **Logs** answer *what happened step by step*: a structured lifecycle log line per
  stage, correlated to the heal trace by `trace_id` so a click moves between them.

When the same heal shows up as a trace, a metric, and a log, the numbers corroborate
each other. That is the point of the dashboard.

## The nine panels

Every panel below is a builder query (no ClickHouse SQL, no raw PromQL). The builder
script re runs each query through `POST /api/v5/query_range` before it creates the
dashboard, so shipping the JSON is proof the query resolves to data.

### Row 0, TRACES: the control loop

| Panel | Type | Query |
|---|---|---|
| Heal cycles run | value | `count()` where `service.name = 'self-healer' AND name = 'agent.heal'` |
| Remediations applied | value | `count()` where `name` is one of the actuator spans (`tool.enable_mitigation`, `tool.disable_fault_injection`, `tool.set_cost_budget`) |
| agent.heal loop breakdown | pie | `count()` grouped by `name` where `service.name = 'self-healer' AND name != 'agent.heal'` |

### Row 1, METRICS: the healer's own instruments

| Panel | Type | Query |
|---|---|---|
| SLO breaches detected | value | `heal.slo.breach` (sum) |
| Heal outcomes (healed true or false) | bar | `heal.result` grouped by `healed` |
| How each fix was decided | pie | `heal.decision` grouped by `source` (memory, llm, fallback) |
| Unsafe actions (must stay 0) | value | `heal.unsafe_action` (sum) |

`How each fix was decided` is the learning loop made visible. When the healer has
already proven a fix for an incident class, it replays that fix from memory with no
model call, and the slice shifts from `llm` to `memory`.

### Row 2, LOGS: the structured lifecycle

| Panel | Type | Query |
|---|---|---|
| Heal lifecycle events by type | bar | `count()` grouped by `heal.event` where `service.name = 'self-healer' AND heal.event EXISTS` |
| Recorded heal outcomes (logs) | pie | `count()` grouped by `heal.healed` where `heal.event = 'outcome'` |

The lifecycle events are `breach.detected`, `decision.recall`, `action.applied`,
`verify`, `rollback`, `escalated`, and `outcome`. Each log carries the active
`trace_id`, so the outcome log line links straight back to its `agent.heal` trace.

## Query Builder techniques on show

* **All three data sources** in one dashboard (traces, metrics, logs).
* **Group by** on span name, on metric attributes (`healed`, `source`), and on log
  attributes (`heal.event`, `heal.healed`).
* **Filter expressions** across services and span names, plus an `EXISTS` predicate and
  an equality on a custom log attribute.
* **Aggregations** with `count()` on traces and logs, and `sum` space aggregation on
  the metric counters.
* **Panel types** matched to the question: `value` for scoreboards, `bar` for
  categorical counts, `pie` for share of a whole.

## Instrumentation behind the panels

Nothing here is synthetic. The signals come from real instrumentation in the repo:

* Traces: [`telemetry.py`](../telemetry.py) sets up the tracer, and every heal stage
  opens a span under `agent.heal` in [`self_heal.py`](../self_heal.py).
* Metrics: [`heal_metrics.py`](../heal_metrics.py) declares the counters and histograms
  (`heal.slo.breach`, `heal.result`, `heal.decision`, `heal.unsafe_action`, and more).
* Logs: `telemetry.py` wires an OTLP `LoggingHandler` onto the root logger with trace
  correlation, and `self_heal.py` emits one structured log per lifecycle step through a
  small `_hlog` helper that maps dotted keys to log attributes.

## Reproduce it

```bash
# 1. bring the loop to life so all three signals have data
python self_heal.py --scenario retry

# 2. build the dashboard (self verifies every panel, then creates it)
python dashboard_fullsignal.py
```

The builder prints an `ok` line per panel with the series count it saw, creates the
dashboard, saves the UUID to `fullsignal_dashboard_uuid.txt`, and writes the importable
JSON to `dashboards/`. Set the dashboard time range to the last three hours to cover a
few heal runs.

To import the shipped JSON into any SigNoz instance instead: Dashboards, then New,
then Import JSON, and pick `dashboards/agent-reliability-three-signals.json`.
