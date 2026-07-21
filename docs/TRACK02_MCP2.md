# Track 02: The MCP Contract Lab

Everyone in this hackathon observes the **agent**. Almost no one observes the
**protocol the agent depends on**. The MCP Contract Lab does that: it is an
OpenTelemetry native reliability certification suite for the Model Context Protocol
itself, and it uses your self hosted SigNoz as the live test oracle.

An MCP server is not "working" because it returned HTTP 200. It is CERTIFIED only when
every reliability contract the lab could actually evaluate held, and any contract the
lab could not evaluate is UNKNOWN, never a silent pass. That is observability as tests,
applied to the wire between an agent and its tools.

| | |
|---|---|
| Service in SigNoz | `mcp-contract-lab` |
| Dashboard | MCP Contract Lab: Reliability Certification (traces + metrics + logs) |
| Dashboard UUID (this instance) | `019f83a2-9faf-7688-9e9b-412c96e61e4e` |
| Importable JSON | [`dashboards/mcp2-contract-lab.json`](../dashboards/mcp2-contract-lab.json) |
| Alerts | MCP Contract Breach (critical), MCP Coverage Blind Spot (warning) |
| Runner | [`mcp2_cert.py`](../mcp2_cert.py) |
| Pure core | [`mcp2_contracts.py`](../mcp2_contracts.py), [`mcp2_model.py`](../mcp2_model.py) |
| Tests | [`tests/test_mcp2_contracts.py`](../tests/test_mcp2_contracts.py) (44, network free) |

## Why this is different

The self healing agent in this repo already proves the closed loop for Track 01. The
Contract Lab takes the same primitives, a network layer that observes and records raw
facts, a pure layer that judges them against deterministic contracts, three state fail
closed verdicts, and points them one layer lower: at MCP, the standard protocol that
now sits between most agents and their tools.

When an MCP server quietly starts returning a bare string instead of a typed content
list, or stops declaring `required` on a schema, or drifts its tool catalog, nothing
crashes. The agent just gets subtly worse, and you find out in production. The Contract
Lab turns that class of silent regression into a graded, alertable, trace backed
verdict on the standard `mcp-contract-lab` service.

## Architecture

```
mcp2_probe.py     network layer: initialize, tools/list, empirical read discovery,
   (observes)     bad-argument + unknown-tool probes, optional in-process fault.
      |           Emits mcp.* CLIENT spans + mcp.client.* metrics per call.
      v
Observation       plain stdlib dataclasses (mcp2_model.py): the raw facts, nothing judged.
      |
      v
mcp2_contracts.py pure layer: 8 contracts, each a function Observation -> Verdict.
   (judges)       No network, no SigNoz. Unit tested on hand built observations.
      |
      v
mcp2_cert.py      the runner: grades the verdicts and emits all three signals to SigNoz,
   (publishes)    then exits non-zero on a FAILED or BLIND grade (a CI gate).
```

The split is the whole point. Detection is deterministic and offline testable, so the
44 unit tests need neither an MCP server nor SigNoz nor the network. Publication is a
thin shell on top.

## The eight contracts

Each contract is a pure function of one captured observation and returns PASS, BREACH,
or UNKNOWN. Ordered by severity (handshake first):

| Contract | PASS means | BREACH means | UNKNOWN means |
|---|---|---|---|
| `initialize_conformance` | handshake completed, protocol declared | initialized with no protocol version | could not connect |
| `advertises_tools` | `tools/list` returned at least one tool | returned zero tools | server unreachable |
| `tool_schemas_wellformed` | every tool declares a usable object schema | a tool declares no usable input schema | no catalog to inspect |
| `result_contract` | a real read returned typed content items | a read returned no typed content | no clean read to inspect |
| `bad_args_handled` | invalid arguments were rejected with a proper error | invalid arguments crashed, hung, or were accepted silently | the probe was not run |
| `unknown_tool_rejected` | a nonexistent tool call was rejected with a proper error | it crashed or returned a fake success | the probe was not run |
| `latency_slo` | read p95 stayed under the SLO (default 8000 ms) | read p95 exceeded the SLO | too few read samples |
| `catalog_stable` | the tool contract matches the pinned baseline | the catalog drifted from the baseline | no baseline pinned yet |

The grade folds the verdicts honestly: **FAILED** if anything breached, **CERTIFIED**
only when everything the lab evaluated passed with no blind spots, **PARTIAL** when it
passed what it saw but some contracts were UNKNOWN, and **BLIND** when nothing was
conclusive at all.

## The three signals it emits

Every certification run writes to SigNoz as all three OpenTelemetry signals, so one
verdict can be read three ways and cross checked.

* **Traces**: one `mcp.cert.suite` root span, a child `cert.<contract>` span per
  contract (ERROR flagged and carrying `mcp2.status = BREACH` on a breach), and
  underneath them the real instrumented `mcp.tools/call`, `mcp.tools/list` and
  `mcp.initialize` client spans of every probe. The auto instrumentation captures the
  actual wire calls with no change to the server.
* **Metrics**: `mcp.cert.contract` (one point per contract per run, labelled by status
  and contract), `mcp.cert.grade` (the overall grade per run), `mcp.cert.blind` (the
  coverage gap counter), and the `mcp.client.calls` and `mcp.client.call.duration`
  auto instrumentation counters.
* **Logs**: a structured, trace correlated `mcp2.*` line per lifecycle step
  (`suite.start`, one `contract` line per verdict, `suite.done`), so the verdict is
  queryable as logs and clicks straight back to its trace.

## The dashboard (Query Builder)

`mcp2_dashboard.py` builds a nine panel dashboard that reads the certification from all
three signals. Every panel is a builder query (no ClickHouse SQL, no raw PromQL), and
the script re runs each query through `POST /api/v5/query_range` before it creates the
dashboard, so shipping [the JSON](../dashboards/mcp2-contract-lab.json) is proof the
query resolves to data.

Highlights: contract verdicts split by status (PASS / BREACH / UNKNOWN), which contract
broke (a single injected fault flips exactly one cell, so the pie points straight at the
defect), read call p95 from the real probe spans, instrumented calls by tool, the grade
per run, the coverage blind spot count, and the certification lifecycle events from
logs.

## The alerts (SigNoz as the pager)

`mcp2_alert.py` ensures two threshold alerts on the ERROR flagged cert spans, so the
same verdict the suite grades on is the signal that pages:

* **MCP Contract Breach** (critical): fires when any span with `mcp2.status = BREACH`
  appears in the window. A previously certified integration just regressed. Verified
  live: a seeded corrupt fault flipped this rule from `inactive` to `firing` within one
  evaluation cycle.
* **MCP Coverage Blind Spot** (warning, notify only): fires when any span with
  `mcp2.status = UNKNOWN` appears. Fail closed: a green run that skipped a check is not
  a trustworthy green run.

## The CI gate

Because detection is deterministic and fail closed, the runner doubles as a CI gate. It
prints a scoreboard and exits non-zero on a FAILED or BLIND grade, so a broken MCP
server fails a pipeline the same way a failing test does:

```
python mcp2_cert.py            # clean:  GRADE CERTIFIED, exit 0
python mcp2_cert.py --fault corrupt:signoz_list_alert_rules   # GRADE FAILED, exit 1
```

## Fault injection (honest chaos)

Faults are injected in process at the client boundary and are always labelled
`fault_injected` on the observation, the same honest chaos style the self healing agent
uses. Each fault flips exactly one contract, which is what makes the red cell a precise
pointer instead of a vague failure:

| Fault | Flips |
|---|---|
| `corrupt:<tool>` (strip content types) | `result_contract` |
| `latency:<tool>:<ms>` (delay a read) | `latency_slo` |
| `drop:<tool>` (raise a transport error) | the read discovery, surfaced as a transport failure |

The `bad_args` and `unknown_tool` probes are never faulted, so their contracts stay a
stable control.

## Drift baseline

`mcp2_baseline.json` pins the certified tool catalog (41 SigNoz MCP tools, fingerprint
`ca5859fcf24f5e03`). It is committed, so `catalog_stable` is meaningful on a fresh
clone: if a future SigNoz release adds, removes, or reshapes a tool, the fingerprint
changes and the contract breaches with a readable added / removed / changed diff. Re pin
deliberately with `python mcp2_cert.py --pin-baseline`.

## Reproduce it

```
# from observable-agent/, with SigNoz on :8080 and the SigNoz MCP server on :8000
python mcp2_cert.py                      # certify the live MCP server, CERTIFIED
python mcp2_cert.py --fault corrupt:signoz_list_alert_rules   # one red cell, FAILED
python mcp2_dashboard.py                 # verify every panel, then create the dashboard
python mcp2_alert.py --ensure            # create the breach + blind spot alerts
python tests/run_all.py                  # 127 unit tests (44 for this lab), no network
```

## Live proof

* Clean run graded **CERTIFIED**, 8 of 8 PASS, exit 0, trace
  `14fb3f1b9fe24c032d43c1c59e0fa11e` on service `mcp-contract-lab`.
* A corrupt fault flipped only `result_contract` to BREACH, graded **FAILED**, exit 1,
  and the **MCP Contract Breach** alert moved to `firing` within one evaluation cycle.
* All three signals confirmed queryable in SigNoz: the `mcp.cert.suite` roots and the
  instrumented `mcp.tools/call` spans, the `mcp.cert.contract` and `mcp.client.*`
  metrics, and the `mcp2.*` lifecycle logs.

## Honest lessons

* **The SigNoz MCP server under declares `required`.** Several read tools
  (`signoz_get_alert`, `get_dashboard`, `get_view`, and others) advertise `required: []`
  but error without an `id`. So "no required arguments" is not enough to know a tool is a
  safe blind read. The probe therefore discovers reads **empirically**: it calls no
  argument candidates, keeps only the ones that come back clean as read samples, and
  guards the whole thing with a mutating name filter so it never calls a `delete_*` or
  `update_*` tool blind. This is a real reliability finding about a live MCP server, made
  by the lab itself.
* **A metric counter alert is the wrong tool for a pass or fail verdict.** The first cut
  alerted on the `mcp.cert.contract` metric and would not fire reliably, because a
  cumulative counter over a short window is awkward for a threshold rule. Switching the
  alert to the ERROR flagged `cert.<contract>` spans (the same proven traces based
  pattern the self healing alerts use) made it fire within one cycle. A reliability
  contract is pass or fail, and a span carries that cleanly.
