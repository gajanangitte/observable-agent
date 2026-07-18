# observable-agent

A tiny **SRE sidekick** AI agent, fully instrumented with **OpenTelemetry** and observed in **self-hosted SigNoz** — traces, tokens, cost, and latency for every LLM call and tool execution.

Built for the *Agents of SigNoz* hackathon (WeMakeDevs × SigNoz). Runs entirely locally on **Ollama** — no API keys, no bill.

> Read the write-up: [`blog/blog.md`](blog/blog.md) — *"I gave a local Llama agent OpenTelemetry eyes."*

---

## 🏆 Competition project: Self-Healing SRE Sidekick

The flagship for **Track T01**. The agent uses SigNoz — through its **MCP
server** — as the sensor in a **closed control loop**: it detects a reliability-SLO
breach, diagnoses it, remediates it, and verifies the fix. The whole cycle
(`detect → diagnose → act → verify`) is one `agent.heal` trace.

In the hero run, a local `qwen2.5:3b` read the incident *from SigNoz*, chose
`disable_fault_injection` itself, and drove the retry-tax SLO from **40% → 0%**
with an **MTTR of 141s** — trace `31514172b44f0f64548eec5c897eb35b`.

```bash
python self_heal.py     # break it → detect via MCP → decide → act → verify via MCP
```

**➡️ Full writeup: [`docs/SELF_HEALING.md`](docs/SELF_HEALING.md)** (architecture,
the hero trace, screenshots, and the honest engineering lessons).

The write-ups this repo was built on:
- [`blog/blog.md`](blog/blog.md) — *"I gave a local Llama agent OpenTelemetry eyes."*
- [`blog/blog2.md`](blog/blog2.md) — *"The Retry Tax"* — chaos, wasted tokens, an alert.
- [`blog/blog3.md`](blog/blog3.md) — *"The agent that reads its own traces"* — the SigNoz MCP server.

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
| `self_heal.py` | **Competition project** — closed-loop orchestrator (`agent.heal` trace) |
| `heal_*.py` | Self-healing modules: sensors (MCP SLO detectors), actuators, control plane, canary rollout, metrics, dashboard |
| `mcp_client.py` | Streamable-HTTP bridge to the SigNoz MCP server |
| `docs/SELF_HEALING.md` | Competition project writeup + hero-run screenshots |
| `blog/` | Blog posts, screenshots, and the SigNoz login/dashboard/capture scripts |

## Notes on honesty

- **Cost is illustrative.** Local Ollama is free; `config.PRICING` applies a per-1M-token rate so cost observability can be demonstrated. The instrumentation is identical to what you'd use against a paid API — point `gen_ai.system` at `openai`/`anthropic` and the same panels track real dollars.
- **CPU inference is slow**, which is a feature here: the latency tail is real, so there's actually something to observe.
