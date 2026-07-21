"""The MCP Contract Lab's network layer: observe a live MCP server, instrumented.

This is the auto-instrumentation core. It opens ONE streamable-HTTP session to an
MCP server and runs a fixed battery of probes:

  * initialize + tools/list  -- the handshake and the advertised tool catalog.
  * read probes              -- a couple of safe, no-argument read tools, called
    for real, to measure latency and check the result content contract.
  * a bad-arguments probe     -- one tool called with its required arguments
    missing, to see whether misuse yields a proper protocol error.
  * an unknown-tool probe     -- a call to a tool that does not exist.

Every one of these MCP interactions is wrapped in a CLIENT span
(``mcp.<method>``) and recorded to the ``mcp.client.*`` metrics, so pointing the
lab at any MCP server instantly produces SigNoz-native traces and metrics for the
protocol, with zero changes to the server. The raw outcomes are returned as a pure
:class:`~mcp2_model.Observation`; judging them is the job of ``mcp2_contracts``.

Fault injection (``fault=`` / ``--fault``) simulates a misbehaving tool at the
client boundary so the certification pipeline can be demonstrated flipping a
contract to BREACH on a genuinely bad reading. It is always recorded on the
observation as ``fault_injected`` so a faulted run is never mistaken for a clean
one. Honest chaos, exactly like the agent's other CHAOS_* knobs.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import List, Optional, Tuple

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

import mcp2_metrics
from mcp2_model import CallProbe, Observation, ToolInfo
from mcp2_contracts import MUTATING_HINTS

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

try:  # McpError location varies across SDK versions
    from mcp import McpError
except Exception:  # noqa: BLE001
    try:
        from mcp.shared.exceptions import McpError
    except Exception:  # noqa: BLE001
        class McpError(Exception):
            pass

tracer = trace.get_tracer("mcp2-contract-lab")

UNKNOWN_TOOL = "__mcp2_nonexistent_tool__"
DEFAULT_PROBE_TIMEOUT_S = 15.0
DEFAULT_READ_K = 3


# --- fault spec ---------------------------------------------------------------
class Fault:
    """A parsed fault directive: ``kind:tool[:arg]``.

    kinds: latency (add ms to the target read call), corrupt (target read returns
    no typed content), drop (target read times out like a wedged tool), none.
    """

    def __init__(self, spec: str = ""):
        self.spec = spec or ""
        self.kind = "none"
        self.tool = ""
        self.ms = 12000.0
        if not spec:
            return
        parts = spec.split(":")
        self.kind = parts[0].strip().lower()
        if len(parts) > 1:
            self.tool = parts[1].strip()
        if self.kind == "latency" and len(parts) > 2:
            try:
                self.ms = float(parts[2])
            except ValueError:
                pass

    def hits(self, tool: str) -> bool:
        return self.kind != "none" and (self.tool == "" or self.tool == tool)


# --- tool selection (generic across any MCP server) ---------------------------
def _required(schema: Optional[dict]) -> List[str]:
    if isinstance(schema, dict):
        req = schema.get("required")
        if isinstance(req, list):
            return [str(x) for x in req]
    return []


def _looks_mutating(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in MUTATING_HINTS)


def read_candidates(tools: List[ToolInfo]) -> List[str]:
    """Tools that MIGHT be safe to call blind: no required arguments AND no mutating
    verb in the name. The name guard is the safety net that keeps us from ever
    calling a ``delete_*`` / ``update_*`` tool with empty arguments, since some MCP
    servers under-declare which arguments are actually required. Deterministic
    (catalog order) so runs are reproducible."""
    return [t.name for t in tools
            if not _required(t.input_schema) and not _looks_mutating(t.name)]


def pick_read_tools(tools: List[ToolInfo], k: int = DEFAULT_READ_K) -> List[str]:
    """First ``k`` read candidates (kept for callers that want a quick guess; the
    probe itself discovers usable reads empirically)."""
    return read_candidates(tools)[:k]


def pick_required_tool(tools: List[ToolInfo]) -> Optional[str]:
    """A NON-mutating tool that declares required arguments, for the missing-args
    probe. A certification run must never risk a real side effect on the server it
    is certifying, so a ``delete_*`` / ``set_*`` / ``write_*`` tool is never chosen;
    if every required-arg tool looks mutating we return None and the caller simply
    skips the misuse probe (an honest gap, never a mutation)."""
    for t in tools:
        if _required(t.input_schema) and not _looks_mutating(t.name):
            return t.name
    return None


# --- one instrumented MCP call ------------------------------------------------
async def _timed_call(session, tool: str, args: dict, label: str, method: str,
                      fault: Fault, timeout_s: float) -> CallProbe:
    p = CallProbe(label=label, tool=tool)
    with tracer.start_as_current_span(f"mcp.{method}", kind=SpanKind.CLIENT) as span:
        span.set_attribute("mcp.transport", "streamable_http")
        span.set_attribute("mcp.method", method)
        span.set_attribute("mcp.tool.name", tool)
        span.set_attribute("mcp.request.bytes", len(json.dumps(args or {})))
        span.set_attribute("mcp2.probe.label", label)
        faulted = fault.hits(tool) and label.startswith("read:")
        if faulted:
            span.set_attribute("mcp2.fault", f"{fault.kind}:{tool}")
        t0 = time.perf_counter()
        try:
            if faulted and fault.kind == "drop":
                raise asyncio.TimeoutError()
            result = await asyncio.wait_for(session.call_tool(tool, args or {}),
                                            timeout=timeout_s)
        except asyncio.TimeoutError:
            p.ok, p.error_class, p.note = False, "transport", "timed out (no response)"
            span.set_status(Status(StatusCode.ERROR, "timeout"))
        except McpError as e:  # a proper protocol error IS a well-formed response
            p.ok, p.is_error, p.error_class = True, True, "protocol"
            p.note = str(getattr(e, "error", e))[:200]
            span.set_attribute("mcp.is_error", True)
        except Exception as e:  # noqa: BLE001  transport / client crash
            p.ok, p.error_class, p.note = False, "transport", f"{type(e).__name__}: {e}"[:200]
            span.set_status(Status(StatusCode.ERROR, p.note))
        else:
            p.ok = True
            p.is_error = bool(getattr(result, "isError", False))
            content = getattr(result, "content", None) or []
            p.content_types = [getattr(c, "type", "unknown") for c in content]
            p.error_class = "tool_error" if p.is_error else "none"
            if faulted and fault.kind == "latency":
                await asyncio.sleep(fault.ms / 1000.0)      # simulate a slow tool
                p.note = f"injected +{fault.ms:.0f}ms latency"
            if faulted and fault.kind == "corrupt":
                p.content_types = []                         # simulate a broken result body
                p.note = "injected corrupt (no typed content)"
            span.set_attribute("mcp.is_error", p.is_error)
            span.set_attribute("mcp.content.types", ",".join(p.content_types) or "none")
        p.latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        span.set_attribute("mcp.latency_ms", p.latency_ms)
        span.set_attribute("mcp.ok", p.ok)
        span.set_attribute("mcp.error.class", p.error_class)
    status = "ok" if (p.ok and not p.is_error) else ("error" if p.is_error else "fail")
    mcp2_metrics.client_call(tool, method, status, p.error_class, p.latency_ms)
    return p


async def _observe_async(url: str, fault: Fault, read_k: int, timeout_s: float,
                         read_tools: Optional[List[str]]) -> Observation:
    obs = Observation(url=url, captured_at=time.time(), fault_injected=fault.spec)
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                with tracer.start_as_current_span("mcp.initialize",
                                                   kind=SpanKind.CLIENT) as span:
                    span.set_attribute("mcp.method", "initialize")
                    span.set_attribute("mcp.server.url", url)
                    init = await asyncio.wait_for(session.initialize(), timeout=timeout_s)
                    obs.reachable = True
                    obs.protocol_version = getattr(init, "protocolVersion", None)
                    si = getattr(init, "serverInfo", None)
                    obs.server_name = getattr(si, "name", None) if si else None
                    span.set_attribute("mcp.protocol.version", obs.protocol_version or "")
                # tools/list
                with tracer.start_as_current_span("mcp.tools/list",
                                                   kind=SpanKind.CLIENT) as span:
                    span.set_attribute("mcp.method", "tools/list")
                    listed = await asyncio.wait_for(session.list_tools(), timeout=timeout_s)
                    for t in listed.tools:
                        obs.tools.append(ToolInfo(
                            name=t.name,
                            input_schema=getattr(t, "inputSchema", None),
                            description=(getattr(t, "description", "") or "")[:200]))
                    span.set_attribute("mcp.tools.count", len(obs.tools))
                    mcp2_metrics.client_call("*", "tools/list", "ok", "none", None)

                await _run_probes(session, obs, fault, read_k, timeout_s, read_tools)
    except Exception as e:  # noqa: BLE001  connect / init failure -> fail closed
        obs.reachable = obs.reachable and False
        obs.probes.append(CallProbe(label="connect", tool="*", ok=False,
                                    error_class="transport",
                                    note=f"{type(e).__name__}: {e}"[:200]))
    return obs


async def _run_probes(session, obs: Observation, fault: Fault, read_k: int,
                      timeout_s: float, read_tools: Optional[List[str]]):
    tool_names = {t.name for t in obs.tools}
    explicit = bool(read_tools)
    candidates = list(read_tools) if explicit else read_candidates(obs.tools)
    # Make sure a read-fault target is actually probed (put it first).
    if fault.kind in ("latency", "corrupt", "drop") and fault.tool in tool_names:
        if fault.tool in candidates:
            candidates.remove(fault.tool)
        candidates.insert(0, fault.tool)

    clean = 0
    cap = read_k + 6                       # bound blind discovery attempts
    for i, name in enumerate(candidates):
        if clean >= read_k or i >= cap:
            break
        p = await _timed_call(session, name, {}, f"read:{name}", "tools/call",
                              fault, timeout_s)
        made = p.ok and not p.is_error
        if explicit or made or fault.hits(name):
            obs.probes.append(p)           # keep clean reads + any faulted sample
        if made:
            clean += 1
    # Guarantee two latency samples for the SLO even if only one clean read exists.
    reads = [p for p in obs.probes if p.label.startswith("read:") and p.ok and not p.is_error]
    if len(reads) == 1:
        name = reads[0].tool
        p = await _timed_call(session, name, {}, f"read:{name}#2", "tools/call",
                              fault, timeout_s)
        obs.probes.append(p)

    req_tool = pick_required_tool(obs.tools)
    if req_tool:
        p = await _timed_call(session, req_tool, {}, "bad_args", "tools/call",
                              Fault(), timeout_s)          # never fault the misuse probe
        p.tool = req_tool
        obs.probes.append(p)

    p = await _timed_call(session, UNKNOWN_TOOL, {}, "unknown_tool", "tools/call",
                          Fault(), timeout_s)
    obs.probes.append(p)


def observe(url: str, fault: str = "", read_k: int = DEFAULT_READ_K,
            timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
            read_tools: Optional[List[str]] = None) -> Observation:
    """Probe one MCP server and return a pure Observation (fail-closed).

    ``read_tools`` optionally pins the exact read tools to sample (deterministic);
    when omitted the probe discovers usable no-argument reads empirically.
    """
    return asyncio.run(_observe_async(url, Fault(fault), read_k, timeout_s, read_tools))
