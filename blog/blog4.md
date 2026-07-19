---
title: "The agent that heals itself: closing the loop with SigNoz"
published: false
tags: signoz, opentelemetry, ai, sre
series: Agents of SigNoz
canonical_url:
---

*This is the fourth and final part of a series where I gave a local Llama agent
real observability with SigNoz running on my own laptop. First I gave it
[OpenTelemetry eyes](https://dev.to/gajanangitte/i-gave-a-local-llama-agent-opentelemetry-eyes-tracing-tokens-cost-and-the-latency-tail-in-4380),
then I measured [the retry tax](https://dev.to/gajanangitte/the-retry-tax-what-a-local-llama-agents-silent-retries-actually-cost-measured-in-self-hosted-3io2)
its silent retries cost, then I wired in the
[SigNoz MCP server so it could read its own traces](https://dev.to/gajanangitte/i-gave-my-local-llama-agent-the-signoz-mcp-server-and-asked-it-to-debug-itself-5909).
This time it does more than watch itself. It heals itself.*

Every "AI plus observability" demo I had seen stopped in the same place: a
beautiful trace of the thing going wrong. The dashboard lights up, the tokens are
gone, and then a human reads it and fixes it by hand.

Observability tells you the agent is on fire. It does not put the fire out.

So for the Agents of SigNoz hackathon I set myself a harder goal. Close the loop.
Detect a reliability breach, diagnose it, act on it, and then prove the fix worked.
All with a local model, all on a laptop, and all grounded in SigNoz rather than the
model's word. No cloud, no API keys.

Here is what I learned building it.

## SigNoz as the sensor, not the dashboard

The whole design rests on one idea. SigNoz plays three roles in a single loop, and
because all three read the same telemetry, they share one source of truth.

1. Sensor. A detector asks SigNoz "did this rollout breach the SLO?" with a
   `signoz_aggregate_traces` query. Deterministic. No LLM involved.
2. Diagnostic surface. When there is a breach, the model reads the incident
   evidence out of the same traces, through the SigNoz MCP server.
3. Scoreboard. After the fix, the loop queries SigNoz again. Breach cleared means
   healed, and that is what sets the MTTR.

That last point is the one I care about most. Detection and verification are
deterministic SigNoz queries. The model is only asked to decide what to do about a
confirmed breach. If it hallucinates, the decision might be wrong, but the breach
and the healed verdict are always facts pulled from telemetry. That is the line
between a party trick and something you would actually let run.

Two services show up in SigNoz, exactly as they would in production: the
`observable-agent` workload (the thing that gets sick) and the `self-healer` (the
loop that watches it, decides, and acts).

## Incident number one: the retry tax

The first breach I chased is the retry tax from part two of the series. A fault
drops the first completed LLM response, so the agent runs the inference again and
spends the tokens twice. It is a perfect target for self healing. It is real
telemetry (a duplicate `llm.chat` span tagged `retry.reason = "response_dropped"`),
it is measurable as an SLO (dropped divided by total per cohort), and there is more
than one way to fix it, so the model has a genuine decision to make.

One command, `python self_heal.py`, runs the whole thing, and the entire cycle is a
single distributed trace.

![The agent.heal closed loop as one trace](https://raw.githubusercontent.com/gajanangitte/observable-agent/main/docs/shots/01_heal_trace.png)

```
agent.heal                                 2.93 min   service = self-healer
  heal.canary.pre     32.9s   BREAK IT    roll out the workload under the fault
  heal.detect          2.2s   DETECT      3x signoz_aggregate_traces (via MCP)
  heal.decide         51.7s   DIAGNOSE and DECIDE  (local qwen2.5:3b)
     llm.chat            26.5s
     tool.read_incident   1.6s   2x signoz_aggregate_traces  (evidence via MCP)
     llm.chat            13.4s
     tool.disable_fault_injection   2ms   the remediation, chosen by the model
  heal.canary.post    86.0s   VERIFY      roll out again under the fixed config
  heal.verify          2.3s   3x signoz_aggregate_traces
```

The model's first move is a `read_incident` tool call, backed by two live
`signoz_aggregate_traces` calls over MCP. The structured evidence it gets back,
retry rate `0.333`, `dropped_and_retried: 2`, and a root cause, comes straight out
of the traces the workload had just emitted.

![read_incident: evidence pulled from SigNoz via MCP](https://raw.githubusercontent.com/gajanangitte/observable-agent/main/docs/shots/03_read_incident.png)

Given that, `qwen2.5:3b` chose `disable_fault_injection` on its own and called it.
Retry rate went from 40% to 0%, MTTR 141 seconds, and every number in that sentence
is a SigNoz query, not a print statement.

## The part that makes it safe: a policy gate

Letting software act on production is only reasonable if the action is constrained.
This is the bit most "autonomous agent" demos skip, and it is the bit I think
actually matters. So every actuator mutation routes through a policy gate before it
can touch anything.

Autonomy levels: `observe`, `suggest`, `approve`, `auto`. In `auto` the gate applies
changes itself. In `approve` it holds them for a human. In `suggest` or `observe` it
only proposes.

Per action risk, reversibility, and blast radius. In `auto` with a low risk cap, low
risk reversible actions apply automatically. A riskier action like `switch_model` is
held for approval instead of being forced through.

An audit line per decision: allowed, held, or denied, with the reason, recorded as a
`heal.policy` metric.

And when an applied fix does not clear the breach, the loop rolls back. It snapshots
the control plane before each attempt and restores it on a failed verify. The retry
incident heals on the first try, so it never needs the rollback, but the machinery
is there, and it is what separates acting from acting safely.

## Incident number two: the bill shock kill switch

The retry tax wastes tokens a few at a time. The failure that actually scares
finance teams is a runaway agent, one stuck in a loop, issuing LLM call after LLM
call, running up an unbounded bill. Same governed loop, a different sensor and a
different fix.

```bash
python self_heal.py --scenario cost
```

The sensor measures calls per request from the traces, with an SLO ceiling of 6. The
model read the runaway from SigNoz, "28 `llm.chat` calls across 2 requests, 14 per
request, versus a max of 6", and chose `set_cost_budget`, arming a per request cost
circuit breaker.

![read_incident: the cost diagnosis pulled from SigNoz via MCP](https://raw.githubusercontent.com/gajanangitte/observable-agent/main/docs/shots/07_cost_incident.png)

That breaker is a real structural cut, not a warning. When a request's cumulative
cost crosses the budget, the agent stops issuing LLM calls and tags the span
`agent.request.severed = true`. Spend per request dropped from $0.000700 to
$0.000123, about an 82% cut, calls per request from 13.5 to 2.5, MTTR 177 seconds.

And here is my favourite frame of the whole project, the `set_cost_budget` span,
with the entire policy decision recorded right on it.

![set_cost_budget: the action the model chose, cleared by the policy gate](https://raw.githubusercontent.com/gajanangitte/observable-agent/main/docs/shots/06_cost_budget_span.png)

`heal.policy.risk = low`, `heal.policy.reversible = true`, `heal.policy.autonomy =
auto`, so `heal.policy.allow = true`. The fix is low risk and reversible, so the gate
lets it through automatically. A riskier action would have stopped and waited for a
human. That is the difference between an agent that can act and one you would let.

## The honest bits

The hackathon asks for real experience, so here is what actually bit me.

Small models need a firm hand to tool call. `llama3.2:3b` kept emitting a malformed
tool call. `qwen2.5:3b`, the same size, does reliable OpenAI format tool calling on
Ollama, so I switched. Even then, the prompt has to spell out that `read_incident`
only reads and must be followed by a remediation, and the output token cap has to
leave room for the decision and the tool call, or the remediation gets truncated.

Keep the LLM out of the reliability hot path. Detection and verification are
deterministic. The model decides. SigNoz judges.

The observer effect is real. The healer's own spans would pollute the workload's SLO
math, so the two run as separate services and every query is scoped to a single
cohort tag.

CPU inference is slow, and that is honest. Each `llm.chat` runs 10 to 110 seconds on
CPU, so a full heal takes a few minutes. The latency tail is real, which means there
is genuinely something to observe, and MTTR means something.

## Why I built it on SigNoz

I could not have built this on a dashboard I could only look at. I needed a backend I
could query programmatically, inside the loop, and one the model could reach through
a standard interface. SigNoz gave me both, the trace store and the MCP server,
runnable on my own laptop, for free. Detection, diagnosis, and verification are all
just SigNoz queries. The loop is built on SigNoz's query surface, not merely pointed
at it.

If you want to run it, it is one command per incident, and the repo ships the exact
`casting.yaml` so you can stand up the same SigNoz deployment:
[github.com/gajanangitte/observable-agent](https://github.com/gajanangitte/observable-agent).

Observability that acts. Turns out the hard part is not the acting. It is proving, in
telemetry, that the fire is actually out.
