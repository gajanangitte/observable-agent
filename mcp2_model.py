"""Plain data model for the MCP Contract Lab (no heavy imports, no network).

The certification core is split the same way the healer is: a NETWORK layer that
observes an MCP server and records raw facts (``mcp2_probe``), and a PURE layer
that judges those facts against deterministic contracts (``mcp2_contracts``).
Everything they hand each other is defined here as stdlib-only dataclasses, so the
contracts are unit-testable on hand-built observations with no MCP server, no
SigNoz and no network at all.

Three verdict states, exactly like the healer's sensors:

  * PASS     -- the contract held.
  * BREACH   -- the contract was violated (the server misbehaved).
  * UNKNOWN  -- the lab could not tell (server unreachable, not enough samples, no
               pinned baseline yet). The critical safety property, identical to the
               healer: a blind check is UNKNOWN, NEVER a silent PASS. The lab never
               certifies what it could not actually observe.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import List, Optional

PASS = "PASS"
BREACH = "BREACH"
UNKNOWN = "UNKNOWN"

# Overall suite grades.
CERTIFIED = "CERTIFIED"   # every contract the lab could evaluate passed, none blind
PARTIAL = "PARTIAL"       # passed what it saw, but some contracts were UNKNOWN
FAILED = "FAILED"         # at least one contract BREACHED
BLIND = "BLIND"           # the lab saw nothing conclusive (e.g. server unreachable)


# --- observation: the raw facts a probe records about one MCP server ----------
@dataclass
class ToolInfo:
    """One advertised MCP tool and its declared input schema."""
    name: str
    input_schema: Optional[dict] = None
    description: str = ""


@dataclass
class CallProbe:
    """The outcome of one probe ``tools/call`` (or a deliberate misuse of one).

    ``ok`` is transport-level: did we get ANY well-formed protocol response back
    (as opposed to a crash, a hang that timed out, or non-JSON). ``is_error`` is
    server-level: did the server report an error PROPERLY (a JSON-RPC error or a
    result with ``isError=true``). The two together let the contracts tell
    "handled the bad input gracefully" apart from "crashed" and from "silently
    accepted garbage".
    """
    label: str                       # why we made this call (latency / bad_args / unknown_tool)
    tool: str
    ok: bool = False
    is_error: bool = False
    latency_ms: Optional[float] = None
    content_types: List[str] = field(default_factory=list)
    error_class: str = "none"        # none | transport | protocol | tool_error
    note: str = ""


@dataclass
class Observation:
    """A full snapshot of one MCP server, captured by ``mcp2_probe.observe``."""
    url: str
    reachable: bool = False
    protocol_version: Optional[str] = None
    server_name: Optional[str] = None
    tools: List[ToolInfo] = field(default_factory=list)
    probes: List[CallProbe] = field(default_factory=list)
    captured_at: float = 0.0
    fault_injected: str = ""         # "" or a label, e.g. "latency:signoz_aggregate_traces"

    # -- helpers --------------------------------------------------------------
    def probe(self, label: str) -> Optional[CallProbe]:
        for p in self.probes:
            if p.label == label:
                return p
        return None

    def read_latencies(self) -> List[float]:
        """Latencies of successful read probes (used by the latency SLO)."""
        return [p.latency_ms for p in self.probes
                if p.label.startswith("read:") and p.ok and not p.is_error
                and p.latency_ms is not None]

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Observation":
        tools = [ToolInfo(**t) for t in d.get("tools", [])]
        probes = [CallProbe(**p) for p in d.get("probes", [])]
        return Observation(
            url=d.get("url", ""), reachable=bool(d.get("reachable", False)),
            protocol_version=d.get("protocol_version"),
            server_name=d.get("server_name"), tools=tools, probes=probes,
            captured_at=float(d.get("captured_at", 0.0)),
            fault_injected=d.get("fault_injected", ""))


# --- verdict: the result of judging an observation against one contract -------
@dataclass
class Verdict:
    contract: str
    status: str
    reason: str
    evidence: dict = field(default_factory=dict)

    @property
    def breached(self) -> bool:
        return self.status == BREACH

    @property
    def known(self) -> bool:
        return self.status in (PASS, BREACH)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# --- drift fingerprint: identity of a server's tool contract ------------------
def _canonical_schema(schema: Optional[dict]) -> str:
    try:
        return json.dumps(schema or {}, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return "<unserializable-schema>"


def catalog_fingerprint(tools: List[ToolInfo]) -> str:
    """A stable hash of the tool catalog (names + input schemas), sorted by name,
    so a drifted contract (a tool added, removed, or whose schema changed) yields
    a different fingerprint. This is the MCP analogue of an API contract hash."""
    parts = []
    for t in sorted(tools, key=lambda x: x.name):
        parts.append(t.name + "=" + _canonical_schema(t.input_schema))
    blob = "\n".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def catalog_diff(baseline: List[ToolInfo], current: List[ToolInfo]) -> dict:
    """Human-readable drift between two tool catalogs (added / removed / changed)."""
    b = {t.name: _canonical_schema(t.input_schema) for t in baseline}
    c = {t.name: _canonical_schema(t.input_schema) for t in current}
    added = sorted(set(c) - set(b))
    removed = sorted(set(b) - set(c))
    changed = sorted(n for n in (set(b) & set(c)) if b[n] != c[n])
    return {"added": added, "removed": removed, "changed": changed}
