"""The observable-agent, turned on itself.

Instead of the fake SRE tools, this entrypoint hands the *same* local Llama a
curated set of SigNoz MCP tools and asks it to debug its OWN performance. Each
run is rooted in an ``agent.introspect`` span whose children are real
``mcp.signoz_*`` calls, so the self-debugging session is itself a trace you can
open in SigNoz -- the agent observing the agent.

    agent.introspect                     (SERVER: the self-debug request)
      |-- llm.chat                       (the model decides what to look at)
      |-- mcp.signoz_aggregate_traces    (reads its own p95 latency by op)
      |-- llm.chat                       (final diagnosis)

Run:  python self_debug.py ["your question"]
Requires the SigNoz MCP server running (see README): TRANSPORT_MODE=http on :8000.
"""
import sys

import config
import telemetry
from agent import Agent
from introspect_tools import build
from mcp_client import SigNozMCP

INTROSPECT_PROMPT = (
    "You are 'observable-agent', an AI agent that can read your OWN performance "
    "telemetry from SigNoz through the provided tools. Every tool returns real "
    "numbers measured from your own OpenTelemetry traces -- never invent figures. "
    "Call a tool to get evidence, then answer. In your diagnosis you MUST compare "
    "your llm.chat (model call) p95 latency against your tool.* p95 latency, cite "
    "both numbers in milliseconds, and state which dominates and by roughly how "
    "many times. End with one concrete way to get faster. Keep it to 2-4 sentences."
)

DEFAULT_QUESTION = (
    "Why are you slow? Read your own recent traces and compare how long your "
    "llm.chat model calls take versus your tool.* calls. Which dominates your "
    "end-to-end latency, and by how much? Give an SRE-ready diagnosis with real "
    "millisecond numbers for both the model calls and the tool calls."
)


def main():
    telemetry.setup_telemetry()
    mcp = SigNozMCP(config.MCP_URL)
    schemas, registry = build(mcp)
    agent = Agent(tool_schemas=schemas, registry=registry,
                  system_prompt=INTROSPECT_PROMPT, root_span="agent.introspect",
                  temperature=0.0)
    q = " ".join(sys.argv[1:]) or DEFAULT_QUESTION
    print(f"\nQ (self): {q}\n")
    try:
        print("A (self-diagnosis):", agent.invoke(q))
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    main()
