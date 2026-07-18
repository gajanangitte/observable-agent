# observable-agent

**Observability that acts.** A local AI agent that doesn't just *watch* itself in
**self-hosted SigNoz** — it **heals** itself: it detects a reliability-SLO breach,
diagnoses it by reading its own telemetry, applies a **policy-gated** fix, verifies
it, and rolls back if the fix doesn't hold. Fully **OpenTelemetry**-native, runs
entirely on **Ollama** — no API keys, no cloud, no bill.

Built for the *Agents of SigNoz* hackathon (WeMakeDevs × SigNoz). It began as an
instrumented SRE sidekick — traces, tokens, cost, and latency for every LLM call
and tool — and grew into a closed control loop that turns observability into action.

> The vision — turning SigNoz-powered observability into action: an open, OTel-native reliability layer that *heals* AI agents on top of the telemetry SigNoz already gives you: [`docs/PITCH.md`](docs/PITCH.md).

---

## 🏆 Competition project: the self-healing control loop

The flagship for **Track T01**. The agent uses SigNoz — through its **MCP
server** — as the sensor in a **governed** closed control loop:
`detect → diagnose → act → verify → rollback`. Every action passes a **policy
gate** first (autonomy levels `observe → suggest → approve → auto`, an action
allow-list, and per-action blast-radius limits), and the whole cycle is one
`agent.heal` trace.

Two incidents, two hero runs — both healed by a local model, both grounded in
telemetry rather than the model's word:

**Retry tax** — a dropped-and-retried LLM response wastes tokens. The model read
the incident *from SigNoz*, chose `disable_fault_injection`, and drove the SLO
from **40% → 0%**, **MTTR 141s** — trace `31514172b44f0f64548eec5c897eb35b`.

**Bill-shock kill-switch** — a runaway agent loops and burns tokens. The model
read the incident and armed a per-request **cost circuit-breaker** that
structurally severs the runaway. Spend/request **$0.000700 → $0.000123**, calls
**13.5 → 2.5** per request (SLO ≤ 6), **MTTR 177s** — trace
`1016e57b2073b2ebedbb013386060b49`. The fix is low-risk + reversible, so in `auto`
mode the policy gate applies it automatically; a medium-risk `switch_model` would
be **held for human approval** instead.

```bash
python self_heal.py                    # heal the retry-tax incident
python self_heal.py --scenario cost    # heal the bill-shock / runaway-spend incident
```

**➡️ Full writeup: [`docs/SELF_HEALING.md`](docs/SELF_HEALING.md)** (architecture,
both hero traces, the governance model, screenshots, and the honest lessons).

The write-ups this repo was built on:
- [`blog/blog.md`](blog/blog.md) — *"I gave a local Llama agent OpenTelemetry eyes."*
- [`blog/blog2.md`](blog/blog2.md) — *"The Retry Tax"* — chaos, wasted tokens, an alert.
- [`blog/blog3.md`](blog/blog3.md) — *"The agent that reads its own traces"* — the SigNoz MCP server.

---

## ▶️ Reproduce it (judges start here)

Everything runs locally — self-hosted SigNoz + a local model. No cloud, no API keys, no bill.

**1. Install SigNoz + its MCP server with Foundry.** This repo ships the exact
[`casting.yaml`](casting.yaml) + [`casting.yaml.lock`](casting.yaml.lock) so you can reproduce the deployment:
```bash
curl -fsSL https://signoz.io/foundry.sh | bash
foundryctl cast -f casting.yaml     # SigNoz UI :8080 · OTLP :4318 · MCP :8000/mcp
```
On Windows, run this inside **WSL 2 with Docker Engine** (not Docker Desktop — ClickHouse Keeper crash-loops under its VM layer).

**2. Start a local model** with [Ollama](https://ollama.com):
```bash
ollama pull qwen2.5:3b              # reliable OpenAI-format tool-calling on Ollama
```

**3. Install the agent:**
```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                             # defaults assume localhost
```

**4. See it *observe*** — emit a trace, then open SigNoz → **Traces**:
```bash
python agent.py "Is the checkout service healthy?"
```

**5. See it *heal*** — the flagship, one command per incident:
```bash
python self_heal.py                    # retry-tax incident:  retry rate 40% → 0%
python self_heal.py --scenario cost    # bill-shock incident: arms a cost kill-switch
```
Each run prints the timeline, MTTR, and a link to the `agent.heal` trace in SigNoz. Full walkthrough with screenshots: [`docs/SELF_HEALING.md`](docs/SELF_HEALING.md).

---

## What it does

The agent answers on-call questions (*"Inventory feels slow — pull its health, recent deploys, and the right runbook."*) using a classic tool-calling loop over four mock SRE tools: `get_service_health`, `list_recent_deploys`, `calculate_error_budget`, `search_runbook`.

Every request emits a three-level span tree, following the OpenTelemetry **GenAI semantic conventions**:

```
agent.invoke        (SERVER)   — one per user question
├─ llm.chat         (CLIENT)   — each model round-trip (gen_ai.* attrs, tokens, cost)
└─ tool.<name>      (INTERNAL) — each tool execution
```

The same LLM numbers are dual-written to **metrics** (`gen_ai.client.token.usage`, `gen_ai.client.cost`, `gen_ai.client.operation.duration`, `agent.requests`, `agent.tool.duration`) so you get both per-request traces and fleet-wide aggregates.

## Prerequisites

1. **SigNoz** (self-hosted). Current installer is [Foundry](https://signoz.io/docs/install/docker/):
   ```bash
   curl -fsSL https://signoz.io/foundry.sh | bash
   # casting.yaml: spec.deployment = { flavor: compose, mode: docker }
   foundryctl cast -f casting.yaml
   ```
   UI on `http://localhost:8080`, OTLP on `:4318`. On Windows, run inside **WSL 2 with Docker Engine** (not Docker Desktop — ClickHouse Keeper crash-loops under its VM layer).
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
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama's OpenAI-compatible endpoint |
| `AGENT_MODEL` | `llama3.2:3b` | Model to run |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | SigNoz OTLP/HTTP ingest |
| `OTEL_SERVICE_NAME` | `observable-agent` | Service name in SigNoz |
| `DEPLOY_ENV` | `dev` | `deployment.environment` resource attr |

## The dashboard

`blog/make_dashboard.py` builds the **LLM & Agent Observability** dashboard (token usage, cost, LLM p95/p99 by model, span latency by operation) via the SigNoz API. It needs an auth token — `blog/signoz_login.py` logs in with Playwright and saves one to `blog/token.txt`.

```bash
pip install playwright && playwright install chromium
python blog/signoz_login.py      # saves blog/state.json + blog/token.txt
python blog/make_dashboard.py    # creates the dashboard, prints its UUID
```

## Repo layout

| File | Purpose |
|---|---|
| `agent.py` | Agent loop + span creation (`agent.invoke`, `llm.chat`, `tool.*`) |
| `telemetry.py` | OpenTelemetry wiring → SigNoz OTLP; metric instruments; `record_*` helpers |
| `tools.py` | Four mock SRE tools + OpenAI tool schemas |
| `config.py` | Env-overridable config + illustrative per-model cost table |
| `run_load.py` | Traffic generator (varied SRE questions, incl. an error path) |
| `self_heal.py` | **Competition project** — governed closed-loop orchestrator (`agent.heal` trace); `--scenario {retry,cost}` |
| `heal_policy.py` | **Governance gate** — autonomy levels + per-action risk/reversibility/blast-radius; the allow-list every mutation passes through |
| `heal_*.py` | Self-healing modules: sensors (MCP SLO detectors: retry tax + cost), actuators (policy-gated, incl. the cost kill-switch), control plane (+ snapshot/rollback), canary rollout, metrics, dashboard |
| `mcp_client.py` | Streamable-HTTP bridge to the SigNoz MCP server |
| `docs/SELF_HEALING.md` | Competition project writeup + hero-run screenshots |
| `blog/` | Blog posts, screenshots, and the SigNoz login/dashboard/capture scripts |

## Notes on honesty

- **Cost is illustrative.** Local Ollama is free; `config.PRICING` applies a per-1M-token rate so cost observability can be demonstrated. The instrumentation is identical to what you'd use against a paid API — point `gen_ai.system` at `openai`/`anthropic` and the same panels track real dollars.
- **CPU inference is slow**, which is a feature here: the latency tail is real, so there's actually something to observe.
