"""Runtime configuration for the observable agent.

Everything is overridable via environment variables so the same code runs
against a local Ollama + self-hosted SigNoz, or a cloud LLM + SigNoz Cloud.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# --- LLM: provider-neutral, OpenAI-compatible (Ollama, vLLM, OpenAI, ...) ------
# The agent talks to ANY OpenAI-compatible chat endpoint. It defaults to a LOCAL
# model (Ollama at :11434) on purpose: the whole loop then runs offline, for free,
# with no telemetry leaving the box -- the right default for a POC and for
# privacy-sensitive observability data. Point GENAI_BASE_URL at vLLM, OpenAI,
# Together, Groq, etc. to run the exact same code against a hosted model.
GENAI_BASE_URL = os.getenv("GENAI_BASE_URL",
                           os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"))
GENAI_API_KEY = os.getenv("GENAI_API_KEY", os.getenv("OLLAMA_API_KEY", "ollama"))
GENAI_SYSTEM = os.getenv("GENAI_SYSTEM", "ollama")   # the gen_ai.system span attribute
MODEL = os.getenv("AGENT_MODEL", "llama3.2:3b")

# Back-compat aliases: older modules referenced these names directly.
OLLAMA_BASE_URL = GENAI_BASE_URL
OLLAMA_API_KEY = GENAI_API_KEY
# Cap the model's response length. Local CPU generation is the slow part, so a
# bound keeps traces snappy without truncating a normal SRE answer.
MAX_OUTPUT_TOKENS = int(os.getenv("AGENT_MAX_OUTPUT_TOKENS", "300"))

# --- Optional cloud escalation tier (opt-in; the agent stays local-first) ------
# Local models are cheap, private and fast enough for the vast majority of
# decisions. For the rare case where the local model's fix does NOT hold on
# verify, the healer can escalate the DECISION (never the detection) to a stronger
# hosted model -- but ONLY if one is configured here. With these unset (the
# default) the agent never makes an off-box call: escalation is disabled and it is
# fully offline. That is the tiered-routing answer to "why a local model?": local
# by default for cost, privacy and latency; cloud on demand, governed and traced.
ESCALATION_MODEL = os.getenv("GENAI_ESCALATION_MODEL", "")
ESCALATION_BASE_URL = os.getenv("GENAI_ESCALATION_BASE_URL", "")
ESCALATION_API_KEY = os.getenv("GENAI_ESCALATION_API_KEY", "")
ESCALATION_SYSTEM = os.getenv("GENAI_ESCALATION_SYSTEM", "openai")
ESCALATION_ENABLED = bool(ESCALATION_MODEL and ESCALATION_BASE_URL and ESCALATION_API_KEY)


def tier(name):
    """Resolve a routing tier to a concrete endpoint. The ``local`` tier is always
    available; ``escalation`` falls back to local unless a cloud tier is fully
    configured, so calling code can ask for escalation without first checking."""
    if name == "escalation" and ESCALATION_ENABLED:
        return {"base_url": ESCALATION_BASE_URL, "api_key": ESCALATION_API_KEY,
                "model": ESCALATION_MODEL, "system": ESCALATION_SYSTEM, "name": "escalation"}
    return {"base_url": GENAI_BASE_URL, "api_key": GENAI_API_KEY,
            "model": MODEL, "system": GENAI_SYSTEM, "name": "local"}

# --- Chaos / reliability experiment knobs ------------------------------------
# EXPERIMENT_ID tags every span + metric so "control" and "chaos" cohorts are
# directly comparable in SigNoz. CHAOS_DROP_RESPONSE_ONCE drops the first
# *completed* model response of each request exactly once (post-inference),
# forcing a retry that duplicates the work -- the "retry tax".
EXPERIMENT_ID = os.getenv("EXPERIMENT_ID", "")
CHAOS_DROP_ONCE = os.getenv("CHAOS_DROP_RESPONSE_ONCE", "0") == "1"
LLM_MAX_ATTEMPTS = int(os.getenv("LLM_MAX_ATTEMPTS", "1"))
RETRY_BACKOFF_MS = int(os.getenv("RETRY_BACKOFF_MS", "250"))

# --- Cost circuit breaker (the "bill-shock" kill-switch) ----------------------
# A hard per-request spend budget. When > 0 and a single agent.invoke's cumulative
# LLM cost reaches it, the agent STRUCTURALLY SEVERS further model calls for that
# request -- a runtime kill-switch -- so a stuck or runaway agent can never run up
# an unbounded bill. 0 disables the guard (default), so behaviour is unchanged.
COST_BUDGET_USD = float(os.getenv("COST_BUDGET_USD", "0") or 0)
# Injected runaway-loop fault (honest chaos, exactly like CHAOS_DROP_RESPONSE_ONCE):
# when on, a request keeps issuing llm.chat "reflection" calls -- burning tokens
# with no new work -- up to CHAOS_RUNAWAY_CALLS, simulating a stuck agent. The cost
# circuit-breaker above is what stops it; without a budget it runs the bill up.
CHAOS_RUNAWAY = os.getenv("CHAOS_RUNAWAY", "0") == "1"
CHAOS_RUNAWAY_CALLS = int(os.getenv("CHAOS_RUNAWAY_CALLS", "12"))

# --- SigNoz MCP server (self-observability) -----------------------------------
# The agent can read its OWN telemetry back out of SigNoz through the official
# SigNoz MCP server. Point this at the server's streamable-HTTP endpoint.
MCP_URL = os.getenv("SIGNOZ_MCP_URL", "http://localhost:8000/mcp")

# --- OpenTelemetry / SigNoz ---------------------------------------------------
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "observable-agent")
ENVIRONMENT = os.getenv("DEPLOY_ENV", "dev")
# Per-attempt OTLP export timeout (seconds) and the bounded flush budget on exit
# (ms). Kept short so a busy local collector never stalls the agent.
OTLP_TIMEOUT_S = int(os.getenv("OTLP_TIMEOUT_S", "5"))
FLUSH_TIMEOUT_MS = int(os.getenv("OTLP_FLUSH_TIMEOUT_MS", "4000"))

# --- Illustrative pricing (USD per 1,000,000 tokens) --------------------------
# The local Ollama model is FREE. These prices let us demonstrate cost
# observability as if the same tokens ran on a hosted model. Swap in your
# provider's real numbers to track actual spend.
PRICING = {
    "llama3.2": (0.10, 0.10),
    "llama3.1": (0.10, 0.10),
    "qwen2.5": (0.10, 0.10),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "default": (0.10, 0.10),
}


def price_for(model: str):
    m = (model or "").lower()
    for key, val in PRICING.items():
        if key != "default" and key in m:
            return val
    return PRICING["default"]


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p_in, p_out = price_for(model)
    return (input_tokens / 1_000_000) * p_in + (output_tokens / 1_000_000) * p_out
