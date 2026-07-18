"""An observable SRE assistant agent.

Every user request becomes one distributed trace:

    agent.invoke                     (SERVER span: the whole request)
      |-- llm.chat                   (CLIENT span: one model round-trip)
      |-- tool.get_service_health    (INTERNAL span: a tool call)
      |-- tool.search_runbook
      |-- llm.chat                   (follow-up round-trip -> final answer)

The LLM runs locally on Ollama; traces/metrics/logs stream to SigNoz via OTLP.
"""
import json
import logging
import sys
import threading
import time

from openai import OpenAI
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

import config
import telemetry
import tools

log = logging.getLogger("agent")
tracer = trace.get_tracer("observable-agent")

SYSTEM_PROMPT = (
    "You are an SRE assistant. Use the available tools to inspect service "
    "health, look up runbooks, list deploys, and compute error budgets. "
    "Call tools when you need facts; never invent metrics. Keep the final "
    "answer concise and actionable."
)

MAX_STEPS = 5


class Agent:
    def __init__(self, tool_schemas=None, registry=None, system_prompt=None,
                 root_span="agent.invoke", temperature=0.1):
        self.client = OpenAI(base_url=config.OLLAMA_BASE_URL, api_key=config.OLLAMA_API_KEY)
        self._chaos = threading.local()
        self._req = threading.local()   # per-request cost/call accumulator (breaker)
        # Default to the built-in SRE tools; the introspection agent injects its
        # own SigNoz-MCP tool set. Behaviour is byte-identical when unset.
        self._tool_schemas = tool_schemas if tool_schemas is not None else tools.TOOL_SCHEMAS
        self._registry = registry if registry is not None else tools.REGISTRY
        self._system_prompt = system_prompt or SYSTEM_PROMPT
        self._root_span = root_span
        self._temperature = temperature

    def _chat(self, messages):
        """One logical LLM round-trip.

        Normally this is a single ``llm.chat`` span. Under injected chaos the
        first *completed* response is dropped (the inference already ran, so the
        tokens are spent) and the agent must retry -- producing a second
        ``llm.chat`` span in the same trace. That duplicated work is the
        "retry tax" the trace makes visible."""
        max_attempts = max(config.LLM_MAX_ATTEMPTS, 2 if config.CHAOS_DROP_ONCE else 1)
        for attempt in range(1, max_attempts + 1):
            with tracer.start_as_current_span("llm.chat", kind=SpanKind.CLIENT) as span:
                span.set_attribute("gen_ai.system", "ollama")
                span.set_attribute("gen_ai.request.model", config.MODEL)
                span.set_attribute("gen_ai.operation.name", "chat")
                span.set_attribute("llm.attempt", attempt)
                if config.EXPERIMENT_ID:
                    span.set_attribute("experiment.id", config.EXPERIMENT_ID)
                start = time.perf_counter()
                try:
                    resp = self.client.chat.completions.create(
                        model=config.MODEL,
                        messages=messages,
                        tools=self._tool_schemas,
                        temperature=self._temperature,
                        max_tokens=config.MAX_OUTPUT_TOKENS,
                    )
                except Exception as e:
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    telemetry.record_llm(config.MODEL, 0, 0, (time.perf_counter() - start) * 1000, "error")
                    raise
                latency_ms = (time.perf_counter() - start) * 1000
                usage = resp.usage
                in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
                out_tok = int(getattr(usage, "completion_tokens", 0) or 0)

                # Injected fault: drop this completed response exactly once. The
                # tokens above were really generated, so we still record them
                # (status="dropped") -- that is the wasted work of the retry.
                if attempt < max_attempts and getattr(self._chaos, "armed", False):
                    self._chaos.armed = False
                    telemetry.record_llm(config.MODEL, in_tok, out_tok, latency_ms, "dropped")
                    telemetry.record_retry(config.MODEL, "response_dropped")
                    backoff_ms = config.RETRY_BACKOFF_MS * attempt
                    span.set_attribute("fault.injected", True)
                    span.set_attribute("retry.reason", "response_dropped")
                    span.add_event("retry.scheduled",
                                   {"retry.reason": "response_dropped",
                                    "retry.backoff_ms": backoff_ms,
                                    "next.attempt": attempt + 1})
                    span.set_status(Status(StatusCode.ERROR,
                                           "response dropped after completion (injected)"))
                    log.warning("llm.chat attempt=%d dropped after completion (injected); "
                                "%d tokens wasted, retrying in %dms",
                                attempt, in_tok + out_tok, backoff_ms)
                    time.sleep(backoff_ms / 1000.0)
                    continue

                cost = telemetry.record_llm(config.MODEL, in_tok, out_tok, latency_ms)
                # Feed the per-request cost circuit-breaker (see invoke()).
                self._req.cost = getattr(self._req, "cost", 0.0) + cost
                self._req.calls = getattr(self._req, "calls", 0) + 1
                choice = resp.choices[0]
                span.set_attribute("gen_ai.response.model", resp.model or config.MODEL)
                span.set_attribute("gen_ai.response.finish_reason", choice.finish_reason or "")
                if attempt > 1:
                    span.set_attribute("llm.was_retry", True)
                if choice.message.content:
                    span.add_event("gen_ai.content.completion",
                                   {"content": choice.message.content[:500]})
                log.info("llm.chat model=%s attempt=%d in=%d out=%d cost=$%.6f %.0fms",
                         config.MODEL, attempt, in_tok, out_tok, cost, latency_ms)
                return choice.message
        # Unreachable: the injection always leaves a final attempt to succeed.
        raise RuntimeError("llm.chat exhausted all attempts")

    def _run_tool(self, name, args):
        with tracer.start_as_current_span(f"tool.{name}", kind=SpanKind.INTERNAL) as span:
            span.set_attribute("tool.name", name)
            span.set_attribute("tool.args", json.dumps(args)[:500])
            start = time.perf_counter()
            fn = self._registry.get(name)
            if not fn:
                span.set_status(Status(StatusCode.ERROR, "unknown tool"))
                telemetry.record_tool(name, (time.perf_counter() - start) * 1000, "error")
                return {"error": f"unknown tool {name}"}
            try:
                result = fn(**args)
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                telemetry.record_tool(name, (time.perf_counter() - start) * 1000, "error")
                return {"error": str(e)}
            latency_ms = (time.perf_counter() - start) * 1000
            status = "error" if isinstance(result, dict) and result.get("error") else "ok"
            telemetry.record_tool(name, latency_ms, status)
            span.set_attribute("tool.result", json.dumps(result)[:500])
            if status == "error":
                span.set_status(Status(StatusCode.ERROR, str(result["error"])))
            log.info("tool.%s args=%s -> %s", name, args, result)
            return result

    def _severed_msg(self):
        return (f"[severed] Cost circuit-breaker tripped: this request reached its "
                f"${config.COST_BUDGET_USD:.6f} budget after {getattr(self._req, 'calls', 0)} "
                f"LLM calls, so further model calls were refused.")

    def _maybe_sever(self, span) -> bool:
        """Return True if the per-request cost budget has been hit. On the first
        breach it stamps the span, emits an event + metric, and logs -- this is the
        structural kill-switch that stops a runaway agent from running up the bill."""
        budget = config.COST_BUDGET_USD
        spent = getattr(self._req, "cost", 0.0)
        if budget <= 0 or spent < budget:
            return False
        if not getattr(self._req, "severed", False):
            self._req.severed = True
            calls = getattr(self._req, "calls", 0)
            span.set_attribute("cost.circuit_broken", True)
            span.set_attribute("cost.budget_usd", round(budget, 6))
            span.set_attribute("cost.spent_usd", round(spent, 6))
            span.add_event("cost.circuit_break", {
                "cost.budget_usd": round(budget, 6),
                "cost.spent_usd": round(spent, 6),
                "llm.calls": calls})
            telemetry.record_cost_break(config.MODEL, spent, budget)
            log.warning("cost circuit-breaker TRIPPED: $%.6f >= budget $%.6f after %d calls; "
                        "severing further llm.chat for this request", spent, budget, calls)
        return True

    def _runaway(self, span, messages):
        """Injected runaway-loop fault: keep issuing llm.chat calls that do no new
        work, so a stuck agent's spend climbs -- until the cost breaker severs it."""
        span.set_attribute("chaos.runaway", True)
        nudge = messages + [{"role": "user",
                             "content": "Reflect further on your answer and continue."}]
        for _ in range(config.CHAOS_RUNAWAY_CALLS):
            if self._maybe_sever(span):
                return True
            self._chat(nudge)   # burns tokens; result intentionally ignored
        return self._maybe_sever(span)

    def invoke(self, question: str) -> str:
        with tracer.start_as_current_span(self._root_span, kind=SpanKind.SERVER) as span:
            span.set_attribute("agent.question", question)
            span.set_attribute("gen_ai.request.model", config.MODEL)
            if config.EXPERIMENT_ID:
                span.set_attribute("experiment.id", config.EXPERIMENT_ID)
            if config.COST_BUDGET_USD > 0:
                span.set_attribute("cost.budget_usd", round(config.COST_BUDGET_USD, 6))
            # Arm the one-shot response-drop for this request (if chaos is on).
            self._chaos.armed = config.CHAOS_DROP_ONCE
            self._req.cost = 0.0
            self._req.calls = 0
            self._req.severed = False
            messages = [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": question},
            ]
            tool_calls_made = 0
            final, status = None, "max_steps"
            try:
                for step in range(MAX_STEPS):
                    # Cost kill-switch: sever BEFORE spending more on this request.
                    if self._maybe_sever(span):
                        final, status = self._severed_msg(), "severed"
                        span.set_attribute("agent.steps", step + 1)
                        break
                    msg = self._chat(messages)
                    if not msg.tool_calls:
                        span.set_attribute("agent.steps", step + 1)
                        final, status = msg.content or "", "ok"
                        break
                    messages.append({
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {"id": tc.id, "type": "function",
                             "function": {"name": tc.function.name,
                                          "arguments": tc.function.arguments}}
                            for tc in msg.tool_calls
                        ],
                    })
                    for tc in msg.tool_calls:
                        tool_calls_made += 1
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        result = self._run_tool(tc.function.name, args)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result),
                        })
                if status == "max_steps":
                    final = "Reached the step limit before producing a final answer."

                # Injected runaway-loop fault runs after the normal answer is ready.
                if config.CHAOS_RUNAWAY and status != "severed" and self._runaway(span, messages):
                    final, status = self._severed_msg(), "severed"

                span.set_attribute("agent.tool_calls", tool_calls_made)
                span.set_attribute("agent.request.llm_calls", getattr(self._req, "calls", 0))
                span.set_attribute("agent.request.cost_usd", round(getattr(self._req, "cost", 0.0), 6))
                if getattr(self._req, "severed", False):
                    span.set_attribute("agent.request.severed", True)
                telemetry.record_request(status)
                span.set_status(Status(StatusCode.OK))
                return final
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                telemetry.record_request("error")
                raise


def main():
    telemetry.setup_telemetry()
    agent = Agent()
    q = " ".join(sys.argv[1:]) or "Is the checkout service healthy? If not, what should I do?"
    print(f"\nQ: {q}\n")
    try:
        print("A:", agent.invoke(q))
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    main()
