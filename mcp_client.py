"""A tiny synchronous bridge to the SigNoz MCP server.

The agent loop is synchronous (the OpenAI/Ollama client is sync), but the MCP
Python SDK is async. Rather than thread an event loop through the whole agent,
each tool call opens a short-lived streamable-HTTP session, runs one JSON-RPC
request, and closes it. The handshake overhead (a few ms on localhost) is
irrelevant next to CPU LLM inference (seconds), and this keeps the integration
dead simple and free of cross-task cancellation hazards.
"""
import asyncio

import config
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class SigNozMCP:
    """Sync facade over the SigNoz MCP server's streamable-HTTP transport."""

    def __init__(self, url: str):
        self.url = url

    def _run(self, coro_fn):
        async def runner():
            async with streamablehttp_client(self.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await coro_fn(session)
        # Bound every call: the fail-closed sensors only convert EXCEPTIONS into the
        # UNKNOWN sentinel, so a wedged server that never replies must raise
        # (TimeoutError) rather than block asyncio.run -- and the heal loop -- forever.
        return asyncio.run(asyncio.wait_for(runner(), timeout=config.MCP_TIMEOUT_S))

    def list_tools(self):
        return self._run(lambda s: s.list_tools()).tools

    def call_tool(self, name: str, args: dict) -> str:
        """Call one MCP tool and return its concatenated text content."""
        result = self._run(lambda s: s.call_tool(name, args or {}))
        parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        text = "\n".join(parts)
        # An MCP tool that FAILED sets isError; its text is an error message, not a
        # result. Raise so the fail-closed sensors convert it to their UNKNOWN sentinel
        # instead of scoring the error string as real data (a false, comforting read).
        if getattr(result, "isError", False):
            raise RuntimeError(text or f"MCP tool '{name}' returned isError with no message")
        return text
