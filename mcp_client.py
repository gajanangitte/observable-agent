"""A tiny synchronous bridge to the SigNoz MCP server.

The agent loop is synchronous (the OpenAI/Ollama client is sync), but the MCP
Python SDK is async. Rather than thread an event loop through the whole agent,
each tool call opens a short-lived streamable-HTTP session, runs one JSON-RPC
request, and closes it. The handshake overhead (a few ms on localhost) is
irrelevant next to CPU LLM inference (seconds), and this keeps the integration
dead simple and free of cross-task cancellation hazards.
"""
import asyncio

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
        return asyncio.run(runner())

    def list_tools(self):
        return self._run(lambda s: s.list_tools()).tools

    def call_tool(self, name: str, args: dict) -> str:
        """Call one MCP tool and return its concatenated text content."""
        result = self._run(lambda s: s.call_tool(name, args or {}))
        parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        return "\n".join(parts)
