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
- [ ] The trigger alert exists: `python heal_alert.py --status` shows the retry tax rule (create it with `python heal_alert.py --ensure --window 5m`).
- [ ] A SigNoz **Alerts** tab open on the retry tax rule, so viewers watch it go Firing then Resolved.
- [ ] A SigNoz **Traces** tab open, filtered to service `self-healer`, ready to refresh.
- [ ] Optional: pre run once so the model is warm in memory (first CPU call is slow).

Layout: terminal on the left ~55%, browser (SigNoz) on the right ~45%.

---

## Beat sheet

### 0:00 to 0:20 · The hook
> "SigNoz already sees my AI agent break and fires an alert. But an alert just
> pages a human. So I wired that alert into a local agent that heals *itself*: the
> alert triggers a governed detect, diagnose, act, verify loop, and then SigNoz
> marks the same alert resolved. The monitoring system opens the incident and
> closes it."

**On screen:** the repo README title, then the SigNoz **Services** list showing the
two services: `observable-agent` (the workload that gets sick) and `self-healer`
(the loop that fixes it).

### 0:20 to 0:45 · The idea in one line
> "SigNoz plays four roles in one loop: the **trigger** (an alert firing wakes the
> healer), the **sensor** that detects the breach, the **diagnostic surface** the
> model reads through the MCP server, and the **scoreboard** that proves the fix.
> Detection and verification are deterministic SigNoz queries. The model only
> *decides*."

**On screen:** the architecture diagram in `docs/SELF_HEALING.md` (or just narrate
over the Services view).

### 0:45 to 2:00 · The closed loop: SigNoz fires the alert, the agent heals it
`heal_bridge.py` is already running, watching SigNoz. Arm a breach in another shell:
```bash
python self_heal.py --break-only     # emit a rollout that breaches the retry tax SLO
```
Narrate as the alert turns Firing and the bridge reacts:
> "The workload just breached. Watch the alert in SigNoz go **Firing**. The bridge
> catches that transition and launches the governed heal on its own, no human. The
> local model reads the incident *straight from the traces* via MCP and, on its own,
> picks a fix. This run it chose `enable_mitigation`; the direct retry run chose
> `disable_fault_injection`. Two different valid fixes on different runs, so the
> choice is real, not scripted. And every action clears a **policy gate** first:
> low risk, reversible, so in auto mode it's allowed."

When the bridge prints the heal result, switch to SigNoz and open the `heal.trigger`
trace:
> "The alert handoff and the entire heal are **one distributed trace**. `heal.trigger`
> at the top is the SigNoz alert; everything under it, detect, decide, the MCP calls,
> the actuator, verify, is the heal it caused. Retry rate **40% to 0%**, heal MTTR
> about **two and a half minutes**. Then SigNoz moves the alert back to **Resolved**:
> the monitoring system closed the loop."

**On screen:** the SigNoz Alerts view going Firing → Resolved, and the `agent.heal`
trace rooted at `heal.trigger` (reference trace `076158eaca24f824a2fb943fd978ca7a`;
`docs/shots/01_heal_trace.png` shows the heal waterfall). Click the `read_incident`
span to show the evidence pulled from SigNoz, and the actuator span.

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

CPU inference makes each `llm.chat` 10 to 110s, so a full heal cycle is a few
minutes, and the alert takes a couple more to fire and then to resolve as the
breaching spans age out of its 5 minute window. Options:
- **Pre arm and pre record.** Start `heal_bridge.py` and the break a few minutes
  before you record, so the alert is already Firing when you begin; cut to the
  finished `heal.trigger` trace (`076158…`, real and reproducible) and the Resolved
  alert.
- **Pre record the direct runs** and cut to the finished traces (IDs `31514172…`
  retry and `1016e57b…` cost).
- Or narrate over the already captured screenshots in `docs/shots/` while a run
  finishes in the background.

Keep it real: show the actual SigNoz UI and the actual trace. No slideware.
