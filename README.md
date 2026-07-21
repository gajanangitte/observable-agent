# observable-agent

**Observability that acts.** A local AI agent that doesn't just *watch* itself in
**self hosted SigNoz**, it **heals** itself: it detects a reliability SLO breach,
diagnoses it by reading its own telemetry, applies a **policy gated** fix, verifies
it, and rolls back if the fix doesn't hold. Fully **OpenTelemetry** native, runs
entirely on **Ollama**. No API keys, no cloud, no bill.

Built for the *Agents of SigNoz* hackathon (WeMakeDevs × SigNoz). It began as an
instrumented SRE sidekick, with traces, tokens, cost, and latency for every LLM call
and tool, and grew into a closed control loop that turns observability into action.

> The vision: turning SigNoz powered observability into action: an open, OTel native reliability layer that *heals* AI agents on top of the telemetry SigNoz already gives you: [`docs/PITCH.md`](docs/PITCH.md).

---

## 🏆 Competition project: the self healing control loop

The flagship for **Track T01**. The agent uses SigNoz, through its **MCP
server**, as the sensor in a **governed** closed control loop:
`detect → diagnose → act → verify → rollback`. Every action passes a **policy
gate** first (autonomy levels `observe → suggest → approve → auto`, an action
allow list, and per action blast radius limits), and the whole cycle is one
`agent.heal` trace.

Two incidents, two hero runs, both healed by a local model, both grounded in
telemetry rather than the model's word:

**Retry tax**: a dropped and retried LLM response wastes tokens. The model read
the incident *from SigNoz*, chose `disable_fault_injection`, and drove the SLO
from **40% → 0%**, **MTTR 141s**, trace `31514172b44f0f64548eec5c897eb35b`.

**Bill shock kill switch**: a runaway agent loops and burns tokens. The model
read the incident and armed a per request **cost circuit breaker** that
structurally severs the runaway. Spend/request **$0.000700 → $0.000123**, calls
**13.5 → 2.5** per request (SLO ≤ 6), **MTTR 177s**, trace
`1016e57b2073b2ebedbb013386060b49`. The fix is low risk + reversible, so in `auto`
mode the policy gate applies it automatically; a medium risk `switch_model` would
be **held for human approval** instead.

```bash
python self_heal.py                    # heal the retry tax incident
python self_heal.py --scenario cost    # heal the bill shock / runaway spend incident
```

**Hands off, SigNoz triggered.** You do not have to launch the heal yourself. A
SigNoz **alert** can be the trigger: `heal_bridge.py` watches the alert, launches the
governed heal inside the alert's own trace when it fires, then waits for SigNoz to
mark it resolved. The alert opens the incident and the alert closes it, one
distributed trace from `heal.trigger` all the way to the healed verdict.

```bash
python heal_bridge.py                  # watch SigNoz and heal when an alert fires
```

**Built to be trusted.** Detection and verification never call the model: sensors are
three state (a blind sensor reports UNKNOWN and refuses to act, never a silent zero),
and robust distribution free statistics (median/MAD z, EWMA, CUSUM) back the SLO floor,
so you do not need a neural net to read a metric. The model decides only what to do
about a confirmed breach, and once SigNoz verifies a fix, the healer remembers it and
replays it on a recurrence with no model call at all. A chaos harness (`eval.py`) drives
the real decision core through adversarial episodes and asserts **zero unsafe actions**.
Local by default (no keys, no egress, no bill), provider neutral so it can escalate only
the hard decision to a hosted model if you want. More in
[`docs/SELF_HEALING.md`](docs/SELF_HEALING.md) under *Built to be trusted*.

**➡️ Full writeup: [`docs/SELF_HEALING.md`](docs/SELF_HEALING.md)** (architecture,
both hero traces, the governance model, screenshots, and the honest lessons).

The write ups this repo was built on:
- [`blog/blog.md`](blog/blog.md): *"I gave a local Llama agent OpenTelemetry eyes."*
- [`blog/blog2.md`](blog/blog2.md): *"The Retry Tax"*: chaos, wasted tokens, an alert.
- [`blog/blog3.md`](blog/blog3.md): *"The agent that reads its own traces"*: the SigNoz MCP server.

---

## ▶️ Reproduce it (judges start here)

Everything runs locally, self hosted SigNoz + a local model. No cloud, no API keys, no bill.

**1. Install SigNoz + its MCP server with Foundry.** This repo ships the exact
[`casting.yaml`](casting.yaml) + [`casting.yaml.lock`](casting.yaml.lock) so you can reproduce the deployment:
```bash
curl -fsSL https://signoz.io/foundry.sh | bash
foundryctl cast -f casting.yaml     # SigNoz UI :8080 · OTLP :4318 · MCP :8000/mcp
```
On Windows, run this inside **WSL 2 with Docker Engine** (not Docker Desktop, ClickHouse Keeper crash loops under its VM layer).

**2. Start a local model** with [Ollama](https://ollama.com):
```bash
ollama pull qwen2.5:3b              # reliable OpenAI format tool calling on Ollama
```

**3. Install the agent:**
```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                             # defaults assume localhost
```

**4. See it *observe***: emit a trace, then open SigNoz → **Traces**:
```bash
python agent.py "Is the checkout service healthy?"
```

**5. See it *heal***: the flagship, one command per incident:
```bash
python self_heal.py                    # retry tax incident:  retry rate 40% → 0%
python self_heal.py --scenario cost    # bill shock incident: arms a cost kill switch
```
Each run prints the timeline, MTTR, and a link to the `agent.heal` trace in SigNoz. Full walkthrough with screenshots: [`docs/SELF_HEALING.md`](docs/SELF_HEALING.md).

---

## What it does

The agent answers on call questions (*"Inventory feels slow, pull its health, recent deploys, and the right runbook."*) using a classic tool calling loop over four mock SRE tools: `get_service_health`, `list_recent_deploys`, `calculate_error_budget`, `search_runbook`.

Every request emits a three level span tree, following the OpenTelemetry **GenAI semantic conventions**:

```
agent.invoke        (SERVER): one per user question
├─ llm.chat         (CLIENT): each model round trip (gen_ai.* attrs, tokens, cost)
└─ tool.<name>      (INTERNAL): each tool execution
```

The same LLM numbers are dual written to **metrics** (`gen_ai.client.token.usage`, `gen_ai.client.cost`, `gen_ai.client.operation.duration`, `agent.requests`, `agent.tool.duration`) so you get both per request traces and fleet wide aggregates.

## Prerequisites

1. **SigNoz** (self hosted). Current installer is [Foundry](https://signoz.io/docs/install/docker/):
   ```bash
   curl -fsSL https://signoz.io/foundry.sh | bash
   # casting.yaml: spec.deployment = { flavor: compose, mode: docker }
   foundryctl cast -f casting.yaml
   ```
   UI on `http://localhost:8080`, OTLP on `:4318`. On Windows, run inside **WSL 2 with Docker Engine** (not Docker Desktop, ClickHouse Keeper crash loops under its VM layer).
2. **Ollama** + a model:
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ollama pull llama3.2:1b   # or llama3.2:3b for better answers, slower on CPU
   ```
3. **Python 3.10+**.

## Quickstart

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                               # adjust if your ports differ

# ask one question
python agent.py "Is the checkout service healthy?"

# or generate a load of varied questions (10 by default)
python run_load.py
```

Then open SigNoz → **Traces** to see the `agent.invoke → llm.chat → tool.*` waterfall, and click any `llm.chat` span to inspect its `gen_ai.*` attributes (model, tokens, cost, finish reason).

## Configuration

All via env vars (see [`.env.example`](.env.example)):

| Var | Default | Purpose |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama's OpenAI compatible endpoint |
| `AGENT_MODEL` | `llama3.2:3b` | Model to run |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | SigNoz OTLP/HTTP ingest |
| `OTEL_SERVICE_NAME` | `observable-agent` | Service name in SigNoz |
| `DEPLOY_ENV` | `dev` | `deployment.environment` resource attr |

## The dashboard

`blog/make_dashboard.py` builds the **LLM & Agent Observability** dashboard (token usage, cost, LLM p95/p99 by model, span latency by operation) via the SigNoz API. It needs an auth token. `blog/signoz_login.py` logs in with Playwright and saves one to `blog/token.txt`.

```bash
pip install playwright && playwright install chromium
python blog/signoz_login.py      # saves blog/state.json + blog/token.txt
python blog/make_dashboard.py    # creates the dashboard, prints its UUID
```

## Cost and economics model (plug and play)

The agent reasons about real dollars: what a token costs, what a minute of
downtime costs your business, and how much a heal saves. Every one of those
numbers lives in [`economics.yaml`](economics.yaml), so you plug in YOUR figures
without touching code. Resolution is layered, lowest to highest precedence:

1. built-in defaults in `economics.py` (real, sourced, dated public figures), so a
   fresh clone and an offline box just work,
2. `economics.yaml` (the single file you edit), and
3. a few `ECON_*` environment variables for quick per run overrides.

It ships with real, cited data you can trust or replace: hosted LLM list prices
(OpenAI, Anthropic, Google, per 1,000,000 tokens) and named cost of downtime
profiles (a Gartner baseline of about 336K USD per hour, plus ITIC 2024
enterprise and regulated profiles). To model your own shop, edit the file or set
`ECON_ACTIVE_DOWNTIME_PROFILE=custom` with your finance team's number.

Every `self_heal.py` run closes with an `[IMPACT]` line that turns the heal into
money at those benchmarks, for example the monthly LLM spend a cost breaker saves
at your request volume, and the cost of the outage class the agent cleared
unattended. The whole surface is covered by `tests/test_economics.py` (network
free).

## GreenOps: energy and carbon per verified answer (plug and play)

Track 03. A token dashboard tells you what you spent. WattTrace tells you what you
BURNED, and how much of it was waste. It drives the real agent over a fixed,
deterministically graded question set and reports one north star: the joules spent per
VERIFIED answer. Retries and failed calls spend real energy for zero extra correct
answers, so that wasted energy shows up as a carbon and cost regression you can alert on.

The physics is a plug and play model, mirroring the economics layer. Everything you
tune for your own hardware and grid lives in [`energy.yaml`](energy.yaml):

1. built-in defaults in `energy.py` (a 65 W desktop CPU proxy, the world grid at
   445 gCO2e per kWh, real cited figures), so a fresh offline clone just works,
2. `energy.yaml` (the single file you edit: hardware tiers, grid regions, the budget), and
3. a few `WATT_*` environment variables for quick per run overrides.

Energy is modelled deterministically from token counts (prefill throughput for the
input, decode throughput for the output) times the tier's active power draw times PUE.
That makes the retry tax exact and reproducible instead of hostage to a noisy CPU wall
clock. The measured wall time is still recorded alongside as a cross check, and you can
switch the basis to `walltime` or feed a real wall meter reading (which is then stamped
MEASURED).

Honesty is built in. Every estimate carries its provenance: a `method` (rapl,
configured, hardware_proxy, or fallback) and a `quality` (MEASURED, ESTIMATED, or
FALLBACK). A run folds to its weakest estimate, so a green board built on a guess is
labelled a guess, never presented as a measurement. The verdict is fail closed and
three state: below a minimum sample it is UNKNOWN, never a false all clear, and a run
with zero verified answers reports UNKNOWN energy per answer, never a comforting zero.

    python watt_report.py                        # control vs a retry fault, compared
    python watt_report.py --fault retry --gate   # exit non-zero if the fault breaches budget

The suite emits the whole run to SigNoz as traces and metrics on the `watttrace`
service, self verifies a nine panel GreenOps dashboard, and ships two alerts (an energy
budget breach and a retry energy waste warning). The pure model and the runner's fail
closed verifier are covered by `tests/test_energy.py` and `tests/test_watttrace.py`
(network free). A separate live smoke, `python tests/test_watttrace_live.py` (opt in with
`WATTTRACE_LIVE_SMOKE=1`), drives the real agent end to end and then queries SigNoz back
to prove the run's trace actually landed. Full writeup:
[`docs/TRACK03_WATTTRACE.md`](docs/TRACK03_WATTTRACE.md).

## Repo layout

| File | Purpose |
|---|---|
| `agent.py` | Agent loop + span creation (`agent.invoke`, `llm.chat`, `tool.*`) |
| `telemetry.py` | OpenTelemetry wiring → SigNoz OTLP; metric instruments; `record_*` helpers |
| `tools.py` | Four mock SRE tools + OpenAI tool schemas |
| `config.py` | Env overridable config; re-exports the pricing surface from `economics.py` |
| `economics.py` | **Plug and play money model**: token prices, cost of downtime, cost SLO, spend budget, and the `[IMPACT]` report; layered defaults → `economics.yaml` → env |
| `economics.yaml` | The one file you edit to plug in your own numbers (real, cited pricing + downtime profiles) |
| `run_load.py` | Traffic generator (varied SRE questions, incl. an error path) |
| `self_heal.py` | **Competition project**: governed closed loop orchestrator (`agent.heal` trace); `--scenario {retry,cost}` |
| `heal_bridge.py` | Turns a **SigNoz alert** into a governed heal (poll + webhook), in the alert's own trace, then watches SigNoz mark it resolved |
| `heal_policy.py` | **Governance gate**: autonomy levels + per action risk/reversibility/blast radius; the allow list every mutation passes through |
| `heal_*.py` | Self healing modules: sensors (MCP SLO detectors: retry tax + cost), actuators (policy gated, incl. the cost kill switch), control plane (+ snapshot/rollback), canary rollout, metrics, dashboard |
| `mcp_client.py` | Streamable HTTP bridge to the SigNoz MCP server |
| `dashboard_fullsignal.py` | **Track 02** builder: one dashboard that reads the heal loop from traces + metrics + logs; self verifies every panel, then exports importable JSON |
| `mcp2_cert.py` | **MCP Contract Lab (Track 02)**: certifies the MCP protocol itself; emits traces + metrics + logs on service `mcp-contract-lab`; exits non-zero on a failed grade (CI gate). `--fault` injects deterministic chaos |
| `mcp2_contracts.py` / `mcp2_model.py` | The pure, network free certification core: eight three state reliability contracts + drift fingerprint (44 unit tests) |
| `mcp2_probe.py` / `mcp2_metrics.py` | Auto instrumentation layer: emits `mcp.*` client spans + `mcp.client.*` metrics per call, empirical safe read discovery, in process fault injection |
| `mcp2_dashboard.py` / `mcp2_alert.py` | The lab's SigNoz dashboard (self verified panels, exported JSON) and its breach + blind spot alerts |
| `energy.py` | **Plug and play GreenOps model**: deterministic token-basis energy, carbon and cost, the fail-closed joules-per-verified-answer verdict, and estimate provenance; layered defaults → `energy.yaml` → env |
| `energy.yaml` | The one file you edit for your hardware and grid (active-watt tiers with provenance, grid carbon regions, the energy budget) |
| `watt_metrics.py` | WattTrace OTel layer: energy / carbon / token / answer / duration instruments + the always-on `on_llm` hook (decoupled, safe no-op offline) |
| `watt_report.py` | **WattTrace GreenOps suite (Track 03)**: drives the agent over a graded set, scores joules per verified answer control vs retry fault, emits traces + metrics on service `watttrace`; `--gate` exits non-zero on a budget breach (CI gate) |
| `watt_dashboard.py` / `watt_alert.py` | The GreenOps SigNoz dashboard (nine self verified panels, exported JSON) and its energy budget breach + retry waste alerts |
| `docs/SELF_HEALING.md` | Competition project writeup + hero run screenshots |
| `docs/TRACK02.md` | Signals and Dashboards writeup: the three signal dashboard, its nine panels, and the Query Builder techniques |
| `docs/TRACK02_MCP2.md` | **MCP Contract Lab** writeup: observability as tests for the MCP protocol, the eight contracts, the three signals, and the alerts |
| `docs/TRACK03_WATTTRACE.md` | **WattTrace GreenOps** writeup (Track 03): joules per verified answer, the honest estimate model, the retry tax as a carbon regression, the dashboard, alerts, and the CI gate |
| `blog/` | Blog posts, screenshots, and the SigNoz login/dashboard/capture scripts |

## Notes on honesty

- **Cost is illustrative by default, real when you want it.** Local Ollama is free; `economics.yaml` applies a per 1M token rate so cost observability can be demonstrated on a laptop. It also ships real, cited hosted prices and cost of downtime benchmarks, so pointing `gen_ai.system` at `openai`/`anthropic` and dropping in your contract rates makes the same panels and the `[IMPACT]` report track real dollars.
- **CPU inference is slow**, which is a feature here: the latency tail is real, so there's actually something to observe.
- **Energy is a model, not a meter.** WattTrace estimates joules from token counts and an active power figure; every number is stamped MEASURED, ESTIMATED, or FALLBACK so a fallback is never shown as a reading. Feed it a RAPL or wall meter value and the same panels track measured energy.
