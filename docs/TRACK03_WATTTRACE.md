# Track 03: WattTrace GreenOps

Everyone in this hackathon counts tokens and dollars. Almost no one counts **joules**.
WattTrace is an OpenTelemetry native GreenOps layer that measures the one number a
token dashboard cannot show you: the energy spent **per verified answer**, and how much
of it was wasted on retries and failed work. It uses your self hosted SigNoz as the
scoreboard and the alarm.

A local agent is not "efficient" because it is cheap to run. It is efficient only when
the energy it burns actually produces correct answers. A retry spends real watts for a
second copy of an answer you already had. A failed call spends watts for nothing. Those
joules are invisible on a cost graph tuned to free local inference, but they are carbon,
and they are the first thing that regresses when an agent gets flaky. WattTrace turns
that waste into a graded, alertable, trace backed verdict.

| | |
|---|---|
| Service in SigNoz | `watttrace` |
| North star metric | joules per **verified** answer (fail closed: UNKNOWN, never a false zero) |
| Dashboard | WattTrace GreenOps: energy per verified answer (traces + metrics) |
| Dashboard UUID (this instance) | `019f83f5-cd63-71ae-9389-a3c5ac351ba0` |
| Importable JSON | [`dashboards/watttrace-greenops.json`](../dashboards/watttrace-greenops.json) |
| Alerts | WattTrace Energy Budget Breach (critical), WattTrace Retry Energy Waste (warning) |
| Runner | [`watt_report.py`](../watt_report.py) |
| Plug and play model | [`energy.py`](../energy.py), [`energy.yaml`](../energy.yaml) |
| Tests | [`tests/test_energy.py`](../tests/test_energy.py) (20) + [`tests/test_watttrace.py`](../tests/test_watttrace.py) (10), network free, plus [`tests/test_watttrace_live.py`](../tests/test_watttrace_live.py) (opt in live end to end) |

## Why this is different

The self healing agent in this repo proves the closed loop for Track 01, and the MCP
Contract Lab certifies the protocol for Track 02. WattTrace takes the same primitives, a
deterministic model that judges raw facts, three state fail closed verdicts, an
alertable SigNoz signal, and points them at a resource nobody else is watching: the
electricity and carbon the agent spends to think.

It reframes reliability as sustainability. The retry tax, the exact fault the "Retry
Tax" blog measured in tokens, is here measured in joules and grams of CO2e. The same
dropped response that wasted 431 tokens also wasted about 249 J, and at fleet scale that
is a line item on a power bill and a number on a carbon report. WattTrace makes the
efficiency of an agent a first class, observable SLO.

## The north star: joules per verified answer

Everything reduces to one number:

```
                 total joules spent by the run
  north star  =  ------------------------------
                    verified answers produced
```

The denominator is the honest part. An answer counts only if a deterministic,
fail closed verifier confirms it stated every required fact (see the golden set in
[`watt_report.py`](../watt_report.py)); a missing or wrong answer is never healthy by
default. So energy spent on work that did not produce a correct answer does not get
amortised away. It inflates the north star, exactly as it should.

When zero answers verify, the north star is **UNKNOWN**, never zero. A run that produced
nothing did not achieve infinite efficiency; it achieved an unknown one. Reporting that
honestly is the whole point.

## The honest estimate model (plug and play)

Energy is a **model**, not a hardware reading, and WattTrace never pretends otherwise.

* **Deterministic token basis (default).** The active compute time of a call is modelled
  from its token counts: the input is prefilled fast and in parallel, the output is
  decoded one slow token at a time. `modelled_seconds = input / prefill_tps + output /
  decode_tps`. Energy is then `active_watts * modelled_seconds * PUE`. This makes the
  retry tax **exact and reproducible**: each dropped call adds precisely its own token
  energy, instead of being swamped by a noisy CPU wall clock (an early wall clock run
  had a cold start swing the regression the wrong way; the token basis fixed it). This
  matches how EcoLogits and CodeCarbon attribute inference energy.
* **Measured wall time is still recorded** on every span as an independent cross check,
  and you can switch the basis to `walltime`, or feed a real wall meter or RAPL reading
  via `WATT_ACTIVE_WATTS`, which is then stamped MEASURED.
* **Provenance on every estimate.** Each figure carries a `method` (rapl, configured,
  hardware_proxy, or fallback) and a `quality` (MEASURED, ESTIMATED, or FALLBACK). A run
  folds to its **weakest** estimate, so a green board built on a guess is labelled a
  guess. A fallback is never shown as a measurement.

The model is a plug and play layer that mirrors the economics layer. Everything you tune
lives in [`energy.yaml`](../energy.yaml), resolved lowest to highest precedence:

1. built-in defaults in `energy.py` (a 65 W desktop CPU proxy, the world grid at
   445 gCO2e per kWh, US electricity at 0.1341 USD per kWh, real cited figures), so a
   fresh offline clone just works,
2. `energy.yaml` (the single file you edit: hardware tiers with active watts and
   provenance, grid carbon regions, throughput, the budget), and
3. `WATT_*` environment variables for quick per run overrides.

Drop in your GPU server's measured draw, your data centre PUE, and your regional grid
factor, and the same panels and alerts report YOUR joules and YOUR carbon.

## The fail closed verdict

The GreenOps SLO is three state, like the agent's sensors and the Contract Lab's
contracts:

* **PASS**: energy per verified answer is at or under the joule budget (default 900 J)
  AND the carbon budget (default 0.11 gCO2e per verified answer).
* **BREACH**: it is over EITHER budget. The cohort span is ERROR flagged and the alert
  pages. Enforcing both means a clean joule number can still breach on carbon, so the
  configurable carbon knob is never dead config.
* **UNKNOWN**: fewer than the minimum verified answers (default 3), OR answers verified
  but no energy was recorded at all (missing token counts, or an accounting failure). A
  zero joule reading is a broken measurement, not free work, so it is UNKNOWN, never a
  comforting PASS. A tiny or unmeasurable cohort can never fire a false breach nor a
  false all clear.

The budget lives in `energy.yaml`. It was calibrated from a real run so a clean cohort
passes and the retry fault breaches (see below).

## Three signals in SigNoz

* **TRACES** on service `watttrace`: one `watt.suite` root per run, a `watt.cohort` span
  carrying the verdict (`watttrace.status`, `watttrace.joules_per_verified_answer`,
  `watttrace.wasted_joules`), and under it every `agent.invoke` with energy tagged
  `llm.chat` children. Each `llm.chat` span carries `watttrace.energy.j`, its
  `watttrace.energy.disposition` (useful or wasted), `watttrace.energy.j_per_token`, and
  the estimate provenance. A dropped retry is stamped `disposition=wasted`.
* **METRICS**: `watttrace.energy.consumed` (J), `watttrace.carbon.emitted` (g),
  `watttrace.answer.count` (labelled verified true or false), `watttrace.token.count`,
  and `watttrace.inference.duration`.
* **LOGS**: the agent's normal trace correlated logs, plus the printed GreenOps
  scoreboard and `[IMPACT]` report.

The always on hook lives in `telemetry.record_llm` and only fires when `WATTTRACE_LIVE`
is set, so the base agent is untouched unless a GreenOps run asks for it. `watt_metrics`
does not import `telemetry` or `agent`, so there is no cycle and it is a safe no-op if
telemetry was never set up.

## The dashboard

[`watt_dashboard.py`](../watt_dashboard.py) builds a nine panel Query Builder dashboard
and **re-runs every panel query through `/api/v5/query_range` before creating it**, so
the script proves each panel resolves on live data, then exports importable JSON:

1. Energy per verified answer, control vs retry fault (the north star, A vs B)
2. Budget breaches (fail closed count)
3. GreenOps verdict mix (PASS / BREACH / UNKNOWN)
4. Joules per token by model (the efficiency knob)
5. Wasted retry energy by cohort (the tax, near zero clean, positive under fault)
6. Energy by disposition (useful vs wasted)
7. Inference latency p95 by model (an independent wall clock cross check)
8. Estimate provenance (MEASURED / ESTIMATED / FALLBACK)
9. Verified vs unverified answers (the north star denominator)

## The alerts

[`watt_alert.py`](../watt_alert.py) ensures two trace based alerts, keyed on the same
span attributes the verdict grades on:

* **WattTrace Energy Budget Breach** (critical): fires when any `watt.cohort` span with
  `watttrace.status = BREACH` appears. The agent got measurably more wasteful.
* **WattTrace Retry Energy Waste** (warning, notify only): fires when any `llm.chat`
  span with `watttrace.energy.disposition = wasted` appears. The tax is climbing even if
  the budget still holds.

## The CI gate

`watt_report.py --gate` exits non-zero when a cohort breaches the budget, so the same
GreenOps verdict SigNoz alerts on can also fail a pull request before the waste ships.
It also fails closed on UNKNOWN: an unjudgeable run (too few verified answers, or no
energy recorded) is never quietly green-lit as a pass.

```
python watt_report.py                        # control vs a retry fault, compared
python watt_report.py --fault retry --gate   # exit non-zero if the fault breaches budget
python watt_report.py --questions 3 --json watt_report.json
```

## Reproduce

```
# 1. Point at your SigNoz (defaults to http://localhost:8080) and drop the API key in
#    .signoz_api_key, then run the suite (drives the real local agent on Ollama):
python watt_report.py

# 2. Build the self verifying dashboard and the alerts:
python watt_dashboard.py
python watt_alert.py --ensure

# 3. The pure model and the fail closed verifier, no network needed:
python tests/run_all.py        # includes test_energy (20) + test_watttrace (10)

# 4. Optional live end to end smoke: drives the real agent, exports to SigNoz, then
#    queries SigNoz back to prove the run's trace landed (skips if the stack is down):
set WATTTRACE_LIVE_SMOKE=1
python tests/test_watttrace_live.py
```

## Live proof

A full five question run on the local `llama3.2:3b` agent
(trace `e66c5e4214b4daa262eb69dfbdb46e81`):

| cohort | verdict | J / verified answer | verified | wasted |
|---|---|---|---|---|
| control (clean) | PASS | 753 J | 4 / 5 | 0 J |
| chaos (retry fault) | BREACH | 1101 J | 4 / 5 | 1375 J |

The retry fault raised energy per verified answer by **46 percent for the same verified
answers**. That extra 1375 J (31 percent of the run, about 0.17 g CO2e on the world
grid) bought zero additional correct answers. It is pure waste, and the budget breach
alert fired on it. An earlier deterministic run
(trace `8df237162415bbbd9cc48c2d770ba4f9`) showed the same shape at +51 percent.

## Novelty defence

There are LLM cost trackers, and there are datacentre power dashboards. WattTrace sits
where neither does: it charges energy against **verified output** on a local, OTel
native, self hosted SigNoz stack, with fail closed honesty (UNKNOWN not zero) and
estimate provenance (a fallback is never shown as measured) baked into the model rather
than bolted on. It makes the retry tax, already told in tokens, into a carbon and cost
SLO you can alert and gate on. That is a GreenOps loop for agents, and it is the same
detect, judge, verify discipline this repo uses everywhere, pointed at the one resource
nobody else in the room is measuring.

## Cross track finale: the carbon verdict becomes a heal sensor

The three tracks in this repo are not three separate demos. They share one telemetry
spine, and this is where they close into a single loop. The WattTrace energy model
(Track 03) is now a **sensor for the self healer** (Track 01), read through the SigNoz
MCP server (the protocol Track 02 certifies).

Run it:

```
python self_heal.py --scenario carbon
```

The healer already knew how to detect a retry SLO breach and clear it. The new
`carbon_slo` sensor in [`heal_sensors.py`](../heal_sensors.py) reads the SAME `llm.chat`
spans, but prices them with the WattTrace energy model in [`energy.py`](../energy.py).
It charges every token to real joules and grams of CO2e, then asks the question a token
dashboard cannot: how much of that energy was WASTED on dropped and retried calls that
served no answer. A retry spends real watts for a second copy of an answer you already
had, so the retry tax that Track 03 measures as wasted carbon is exactly what the healer
now detects and closes.

The breach signal is the **share of the cohort's inference energy wasted on retries**,
held to the same 5 percent floor the retry SLO uses. That choice is deliberate and
calibration free. A healthy cohort wastes zero energy, so it always passes, which means a
verified fix can never be rolled back by ordinary run to run token noise. Gating instead
on an absolute joules per answer budget would be fragile here: this SRE agent makes
several reasoning and tool calls per answer, so its healthy footprint (about 867 J per
answer measured live) sits just under the single call WattTrace reference budget of 900 J,
with no safe margin for a verify step. The total footprint per answer is still reported,
and it falls as the waste is removed, for the dashboard and the impact story.

Because the mechanism is the same wasted retry, the SAME fix heals it. The carbon
scenario seeds the same broken state as the retry scenario and hands the model the same
mitigations (`disable_fault_injection`, `enable_mitigation`), each still cleared by the
policy gate before it can act. The loop is unchanged: detect the breach through MCP,
let the local model read the incident and pick a fix, apply it behind the gate, then
re verify the carbon SLO through MCP and record the footprint drop. A fix that does not
drive the wasted energy back under the floor is rolled back, and a run with answers but
no recorded token energy is UNKNOWN, never a false green.

Two new histograms land on the `self-healer` service so SigNoz shows the footprint fall
as the healer works: `heal.energy.joules_per_answer` (J) and
`heal.carbon.grams_per_answer` (g), each tagged `phase` pre and post. The same figures
carry the fail closed honesty of the rest of the stack: a blind sensor (MCP down) is
UNKNOWN and the loop refuses to act, and the carbon verdict never reports a comforting
zero for a broken meter.

The result is one story a judge can follow end to end: a reliability fault (Track 01)
is also a sustainability regression (Track 03), detected and healed over the open
protocol (Track 02), on a self hosted SigNoz stack, with governance and rollback around
every action. Covered by [`tests/test_greenops.py`](../tests/test_greenops.py) (8,
network free): a wasted energy breach, a clean pass, a heavy but retry free pass that
proves the SLO gates on waste not on an absolute cap, a sub threshold pass, the three
fail closed UNKNOWN paths (zero energy, no answers yet, MCP down), plus a check that the
sensor's totals
match the WattTrace model exactly so the heal SLO and the Track 03 verdict never drift.
