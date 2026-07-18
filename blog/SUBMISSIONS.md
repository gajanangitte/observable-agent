# Submission blurbs — Agents of SigNoz

Ready-to-paste copy for the submission form (https://forms.gle/wf9tFYcksrk6P4Zy8),
the blog platform, and Social Buzz. All three blogs share one repo and one live
self-hosted SigNoz.

**PUBLISHED (account: gajanangitte):**
- Blog #1 (eyes): https://dev.to/gajanangitte/i-gave-a-local-llama-agent-opentelemetry-eyes-tracing-tokens-cost-and-the-latency-tail-in-4380
- Blog #2 (retry tax): https://dev.to/gajanangitte/the-retry-tax-what-a-local-llama-agents-silent-retries-actually-cost-measured-in-self-hosted-3io2
- Blog #3 (MCP self-debug): https://dev.to/gajanangitte/i-gave-my-local-llama-agent-the-signoz-mcp-server-and-asked-it-to-debug-itself-5909
- Repo: https://github.com/gajanangitte/observable-agent

---

## Blog #1 — "I gave a local Llama agent OpenTelemetry eyes" (Early-Win)

**Hook:** If you can't observe your AI agent, you don't own it. Here's the whole
span tree — LLM calls, tokens, cost, latency — for a local agent, in self-hosted
SigNoz.

**Abstract (~55 words):**
I self-hosted SigNoz and built a tiny SRE-sidekick agent on a local Llama
(Ollama, no API keys). Every request emits an `agent.invoke → llm.chat → tool.*`
span tree using the OpenTelemetry GenAI semantic conventions, with tokens, cost,
and latency dual-written to metrics. Then I built a 9-panel LLM observability
dashboard. Real commands, real numbers, one honest walkthrough.

**Tags:** `signoz` `opentelemetry` `observability` `llm` `ai-agents`

**Social Buzz post:**
> If you can't observe your AI agents, you don't own them. 👀
>
> I gave a local Llama agent OpenTelemetry eyes and watched every LLM call,
> token, and dollar land in self-hosted @SigNozHQ — no API keys, all on my
> laptop. Full walkthrough 👇 https://dev.to/gajanangitte/i-gave-a-local-llama-agent-opentelemetry-eyes-tracing-tokens-cost-and-the-latency-tail-in-4380
>
> #AgentsOfSigNoz @wemakedevs

---

## Blog #2 — "The Retry Tax"

**Hook:** A single dropped-and-retried LLM response spent 431 tokens twice. Here's
how I made that waste show up as a span in SigNoz — and alerted on it.

**Abstract (~55 words):**
I injected a chaos fault that drops the *first completed* LLM response, forcing a
retry that re-spends the tokens. Running control-vs-chaos cohorts in self-hosted
SigNoz, the "retry tax" was measurable: +50% `llm.chat` spans, 4,393 wasted
tokens (38%), +32% latency. I built a dashboard and a v5 alert rule that fires on
`retry.reason='response_dropped'`. Real numbers from ClickHouse.

**Tags:** `signoz` `opentelemetry` `llm` `observability` `reliability`

**Social Buzz post:**
> One dropped LLM response = tokens paid for twice. I call it the retry tax. 🧾
>
> I injected the fault, ran control-vs-chaos cohorts, and watched 4,393 wasted
> tokens (38%!) show up as duplicate spans in self-hosted @SigNozHQ — then
> alerted on it. 👇 https://dev.to/gajanangitte/the-retry-tax-what-a-local-llama-agents-silent-retries-actually-cost-measured-in-self-hosted-3io2
>
> #AgentsOfSigNoz @wemakedevs

---

## Blog #3 — "The agent that reads its own traces" (Flagship blog)

**Hook:** I wired the SigNoz MCP server into my agent so a local Llama could query
its *own* traces. It correctly diagnosed itself: "the model calls are ~67,000×
slower than the tools."

**Abstract (~60 words):**
I connected the SigNoz MCP server (41 tools, streamable-HTTP) to a local agent so
it could introspect its own telemetry. Asked to find its bottleneck, it called
`signoz_aggregate_traces` and reported `llm.chat` p95 = 101,375 ms vs tools 1.5 ms
— and the self-debug session is itself a trace. Includes the honest bits: a
malformed tool call, a hallucination, and how I grounded it by letting SigNoz do
the math.

**Tags:** `signoz` `mcp` `llm` `opentelemetry` `ai-agents`

**Social Buzz post:**
> What if an AI agent could read its OWN traces? 🤯
>
> I wired the @SigNozHQ MCP server into a local Llama. Asked for its bottleneck,
> it queried its own telemetry and nailed it: model calls ~67,000× slower than
> tools. The self-debug session is itself a trace. 👇 https://dev.to/gajanangitte/i-gave-my-local-llama-agent-the-signoz-mcp-server-and-asked-it-to-debug-itself-5909
>
> #AgentsOfSigNoz @wemakedevs

---

## Competition project — "The self-healing control loop" (Track T01)

**Hook:** Most AI-observability demos stop at *observe*. This one closes the loop
to *act* — safely. Using SigNoz (via MCP) as sensor, diagnostic surface, and
scoreboard, a local model heals two real incidents through a policy gate.

**Abstract (~65 words):**
A local agent uses self-hosted SigNoz through its MCP server as a *governed*
closed control loop: `detect → diagnose → act → verify → rollback`, all one
`agent.heal` trace. Every fix clears a policy gate (autonomy + risk +
reversibility). It heals a retry-tax breach (40% → 0%, MTTR 141s) and a runaway
bill-shock loop — arming a cost circuit-breaker that cuts spend/request $0.000700
→ $0.000123 (MTTR 177s). Detection/verification are deterministic MCP queries;
the LLM only decides.

**Tags:** `signoz` `mcp` `self-healing` `sre` `ai-agents` `opentelemetry`

**Social Buzz post:**
> Observability that *acts*. 🔧
>
> My agent uses self-hosted @SigNozHQ via MCP as a governed control loop — detect
> → diagnose → act → verify → rollback, all one trace. A local model read a
> runaway-spend incident FROM SigNoz and armed a cost kill-switch: spend/request
> cut ~82%, every action through a policy gate. 👇
> https://github.com/gajanangitte/observable-agent
>
> #AgentsOfSigNoz @wemakedevs
