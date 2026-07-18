# Sentinel — the control plane that *heals* AI agents

> **Datadog shows you the fire. We put it out — safely, autonomously — for the agentic era.**

*Seed one-pager · working prototype: [`observable-agent`](../README.md) (this repo)*

---

## The problem

Enterprises are shipping AI agents into production faster than they can operate
them. Agents fail in ways traditional software doesn't: they retry silently and
double-spend tokens, loop and run up an unbounded bill, drift, hallucinate tool
calls, and cascade across multi-step graphs. Today's answer is **passive**:
LLM-observability tools (SigNoz, Langfuse, Arize, LangSmith) show you the trace
*after* the money is gone and the customer is angry. Someone still has to wake up,
read the dashboard, and fix it by hand.

**Observability tells you the agent is on fire. It doesn't put the fire out.**

## The insight

The value is moving one layer up — from *watching* agents to *acting* on them.
The winners in classic infra weren't the metric stores; they were the control
planes that **closed the loop**: detect → diagnose → **act** → verify. That layer
doesn't exist for AI agents yet, and it can't safely be a black box: acting on
production needs **policy, blast-radius limits, rollback, and an audit trail**.

## The product

**Sentinel is an open, OpenTelemetry-native reliability control plane for AI
agents.** It sits *above* any telemetry backend you already run and turns
observability into action:

- **Senses** agent-native SLOs from live traces (retry tax, runaway-spend, latency, tool-error, task-success).
- **Diagnoses** with a model that reads the incident evidence out of your own telemetry.
- **Acts within policy** — a governed `detect → diagnose → act → verify → rollback` loop with autonomy levels (observe → suggest → approve → auto), an action allow-list, blast-radius limits, and a runtime **cost kill-switch**.
- **Proves it** — re-queries the same telemetry to confirm the fix, and records an immutable audit trail of *what changed, why it was allowed, and whether it worked.*

We don't replace your metric store. We own the **intelligence + action** layer on top of it — local-first and private, so regulated and self-hosted teams can run it too.

## Why now

- **Adoption just crossed the line.** ~1 in 3 enterprises report agentic AI *in production*, projected to jump sharply within 12 months (Battery Ventures, Dec 2025). The pain is now, not hypothetical.
- **Spend is the C-suite's #1 agent fear** — unbounded, non-deterministic token bills. A hard, provable cost kill-switch is a budget-line purchase.
- **Standards converged.** OpenTelemetry GenAI semantic conventions + MCP mean the *instrumentation* is now commodity — so the durable value is the layer that reasons and acts on it.

## Market

Agent/LLM observability is an estimated **~$0.55B (2025) → ~$2.05B by 2030 (~30% CAGR)** — and that's just the *watching* slice. Reliability + autonomous remediation for agents is a larger, adjacent budget (it comes out of SRE/platform, not just monitoring).

## Competition & the wedge

| Segment | Examples | What they do | The gap we fill |
|---|---|---|---|
| LLM/agent observability | SigNoz, Langfuse, Arize, LangSmith | **Watch** agents; dashboards, evals | Passive — no governed action, no rollback |
| Autonomous SRE | Resolve.ai (reported ~$1B), Cleric, Traversal | Fix **human infra** with closed, hosted models | Not agent-native; closed & cloud-only |
| Agent frameworks | LangChain, CrewAI | **Build** agents | Don't operate what they build |

**Nobody heals *your own agents*, openly, with policy + rollback.** SigNoz already ships 40+ LLM integrations and an MCP server — so "add LLM traces to SigNoz" is a dead pitch. The open space is the **governed action layer above** any backend. We're OTel/MCP-native, so their ecosystem is our distribution, not our competitor.

## Moat

Instrumentation (OTel/MCP) is distribution, not defensibility. Our moat compounds:

1. **Incident-evidence corpus** — a growing, labeled library of real agent failures + the remediations that verifiably worked. Every heal makes diagnosis better.
2. **Policy-constrained action** — the trust layer (allow-list, blast radius, approvals, rollback, audit) that makes teams comfortable letting software act. Hard to retrofit onto a passive tool.
3. **Measured outcomes** — we sell a number (failure-rate ↓, MTTR ↓, $ saved), not a dashboard. Value is provable and sticky.

## Proof it works — today

The prototype in this repo runs the full loop on a **laptop** (self-hosted SigNoz + a local model, no cloud, no API keys):

- **Retry-tax incident (live):** detect → the local model reads the incident via MCP → applies a policy-gated fix → verify. **Retry rate 40% → 0%, MTTR ~141s**, the whole cycle a single distributed trace.
- **Bill-shock incident:** a runaway agent trips a per-request **cost circuit-breaker** that structurally severs the LLM connection — a hard kill-switch, verified in telemetry.
- Every action passes a **policy gate** (autonomy level + risk + reversibility), snapshots state, and **rolls back automatically** if the fix doesn't verify.

## Go-to-market

- **ICP:** platform/SRE teams running agents in production who are **self-hosted or regulated** (fintech, healthcare, gov, EU data-residency) — they *can't* send prompts to a hosted SaaS and need local-model reasoning + auditable actions. Underserved by every cloud-only competitor.
- **Wedge feature:** the cost kill-switch (fast "yes", clear ROI) → land → expand into full remediation.
- **Motion:** open-source core (developer trust + adoption) → paid governance, multi-agent graph remediation, team audit/compliance, and managed control plane.

## The ask

Raising a **seed round** to turn the prototype into a product: hire 3–4 engineers,
harden the policy/rollback/audit layer, ship connectors for the top agent
frameworks, and land 5 design-partner deployments.

**12-month milestones:** (1) production-grade governed remediation with signed
audit logs; (2) 5 paying design partners in regulated verticals; (3) an
incident-evidence corpus that measurably improves diagnosis; (4) published
benchmarks — failure-rate and MTTR reductions on real agent workloads.

---

*Built by Gajanan Gitte. The working prototype, architecture, and honest
engineering notes are in this repository.*
