# Demo video script: Self Healing SRE Sidekick

*Target: 3 minutes (hard cap 5). One screen recording, terminal + browser side by
side. Everything is live and local, self hosted SigNoz in WSL2, the model on
Ollama. No cloud, no API keys.*

---

## Preflight checklist (do this before you hit record)

- [ ] SigNoz up: `http://localhost:8080` loads, and Services shows `observable-agent` + `self-healer`.
- [ ] MCP server healthy: `curl http://localhost:8000/livez` → `200`.
- [ ] Ollama up with the model: `ollama list` shows `qwen2.5:3b`.
- [ ] Terminal in the repo, venv active, `AGENT_MODEL=qwen2.5:3b` (or set in `.env`).
- [ ] A SigNoz **Traces** tab open, filtered to service `self-healer`, ready to refresh.
- [ ] Optional: pre run once so the model is warm in memory (first CPU call is slow).

Layout: terminal on the left ~55%, browser (SigNoz) on the right ~45%.

---

## Beat sheet

### 0:00 to 0:20 · The hook
> "Observability tells you your AI agent is on fire. It doesn't put the fire out.
> Someone still has to wake up and fix it. So I wired a local agent into
> self hosted **SigNoz** and taught it to heal *itself*: detect, diagnose, act,
> verify, and roll back if the fix doesn't hold."

**On screen:** the repo README title, then the SigNoz **Services** list showing the
two services: `observable-agent` (the workload that gets sick) and `self-healer`
(the loop that fixes it).

### 0:20 to 0:45 · The idea in one line
> "SigNoz plays three roles in one loop: it's the **sensor** that detects the
> breach, the **diagnostic surface** the model reads through the MCP server, and
> the **scoreboard** that proves the fix. Detection and verification are
> deterministic SigNoz queries. The model only *decides*."

**On screen:** the architecture diagram in `docs/SELF_HEALING.md` (or just narrate
over the Services view).

### 0:45 to 2:00 · Incident #1: the retry tax (live heal)
Run it:
```bash
python self_heal.py
```
Narrate as the phases print:
> "It rolls out the workload under a fault that drops and retries an LLM response.
> That's wasted tokens. The sensor queries SigNoz and confirms the breach: retry
> rate **40%**. The local model reads the incident *straight from the traces* via
> MCP, and on its own picks `disable_fault_injection`. But it doesn't just fire.
> The action clears a **policy gate** first: low risk, reversible, so in auto mode
> it's allowed. Then it rolls out again and queries SigNoz again to verify."

When it finishes, switch to SigNoz, refresh Traces, open the `agent.heal` trace:
> "The entire heal cycle is **one distributed trace**: detect, decide, the MCP
> calls, the actuator, verify. Retry rate **40% → 0%**, MTTR about **two and a half
> minutes**. Healed, and proven in telemetry."

**On screen:** the `agent.heal` waterfall (`docs/shots/01_heal_trace.png` is the
reference), then click the `read_incident` span to show the evidence pulled from
SigNoz, and the `disable_fault_injection` span.

### 2:00 to 2:40 · Incident #2: the bill shock kill switch (the money shot)
Run it:
```bash
python self_heal.py --scenario cost
```
> "The scarier failure is a **runaway agent**, stuck in a loop, burning tokens,
> running up an unbounded bill. Same governed loop, different sensor: it measures
> calls per request from the traces. The model reads the runaway from SigNoz and
> arms a per request **cost circuit breaker**, a hard kill switch that
> structurally severs any request that hits the budget."

Open the `set_cost_budget` span in SigNoz:
> "Look at the span. The whole decision is on it: risk low, reversible true,
> autonomy auto → **allowed**. Spend per request drops from **$0.000700 to
> $0.000123**, about an 82% cut. A *medium* risk action like switching the model
> would have been **held for human approval** instead."

**On screen:** `docs/shots/06_cost_budget_span.png`: the `heal.policy.*` gate
attributes + the kill switch effect.

### 2:40 to 3:00 · Why SigNoz, and close
> "Nothing here is scripted around the model. The breach and the healed verdict
> are always grounded in SigNoz, not the model's word. That's what makes it safe
> enough to let software act. It all runs on a laptop, on top of SigNoz and
> OpenTelemetry. One command per incident. Thanks for watching."

**On screen:** back to the `agent.heal` trace, then the repo URL
`github.com/gajanangitte/observable-agent`.

---

## If a live run is too slow on the day

CPU inference makes each `llm.chat` 10 to 110s, so a full cycle is a few minutes.
Options:
- **Pre record the two runs** and cut to the finished traces (honest, the traces
  are real, IDs `31514172…` retry and `1016e57b…` cost).
- Or narrate over the already captured screenshots in `docs/shots/` while one run
  finishes in the background.

Keep it real: show the actual SigNoz UI and the actual trace. No slideware.
