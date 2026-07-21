"""The MCP reliability contracts: deterministic, fail-closed certification checks.

Each contract is a PURE function of a captured :class:`~mcp2_model.Observation`
(the raw facts a probe recorded) and returns a three-state :class:`Verdict`
(PASS / BREACH / UNKNOWN). There is no network here and no MCP server here, so the
whole certification judgement is reproducible and unit-testable on hand-built
observations. This is the "observability as tests" core: an MCP server is not
"working" because it returned 200, it is CERTIFIED only when every contract the
lab could evaluate held, and any contract the lab could not evaluate is UNKNOWN,
never a silent pass.

The contracts, in order of severity:

  * initialize_conformance -- the server completes the MCP handshake and declares
    a protocol version.
  * advertises_tools       -- tools/list returns at least one tool.
  * tool_schemas_wellformed-- every advertised tool declares a JSON-Schema object
    input (name + typed schema), so a client can actually call it safely.
  * result_contract        -- a real tools/call returns the MCP content contract
    (a non-empty list of typed content items), not a bare string or nothing.
  * bad_args_handled       -- calling a tool with invalid arguments returns a
    PROPER protocol error, it does not crash, hang, or silently accept garbage.
  * unknown_tool_rejected  -- calling a tool that does not exist returns a proper
    error, not a hang or a fake success.
  * latency_slo            -- p95 of representative read-only calls stays under the
    latency SLO.
  * catalog_stable         -- the tool contract still matches the pinned baseline
    (no undeclared drift).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from mcp2_model import (
    BREACH, PASS, UNKNOWN, CERTIFIED, PARTIAL, FAILED, BLIND,
    Observation, ToolInfo, Verdict, catalog_fingerprint, catalog_diff,
)

# Default latency SLO for a representative read-only MCP call. Read tools (list /
# get style) should answer well under this even on a busy local box; a genuinely
# slow or wedged tool blows past it. Override via MCP2_LATENCY_SLO_MS.
DEFAULT_LATENCY_SLO_MS = 8000.0
MIN_LATENCY_SAMPLES = 2

# Names that clearly mutate state; never auto-probed for latency / result shape.
MUTATING_HINTS = ("create", "update", "delete", "set", "write", "remove", "add",
                  "drop", "put", "patch", "post", "enable", "disable", "run",
                  "execute", "send", "trigger", "install", "apply")


@dataclass
class CertConfig:
    """Knobs + the pinned baseline the contracts judge against."""
    latency_slo_ms: float = DEFAULT_LATENCY_SLO_MS
    baseline_tools: Optional[List[ToolInfo]] = None   # None -> no baseline pinned yet


def _pctl(xs: List[float], p: float) -> float:
    """Nearest-rank percentile (same convention as the eval harness)."""
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def _schema_wellformed(schema: Optional[dict]) -> bool:
    """A usable MCP input schema is a JSON-Schema object: a dict that is either
    typed ``object`` or declares ``properties`` (an empty object schema counts;
    a missing / non-dict / non-object schema does not)."""
    if not isinstance(schema, dict):
        return False
    if schema.get("type") == "object":
        return True
    return "properties" in schema


# --- the contracts (pure: Observation + CertConfig -> Verdict) ----------------
def initialize_conformance(obs: Observation, cfg: CertConfig) -> Verdict:
    if not obs.reachable:
        return Verdict("initialize_conformance", UNKNOWN,
                       "could not connect to or initialize the MCP server",
                       {"url": obs.url})
    if obs.protocol_version:
        return Verdict("initialize_conformance", PASS,
                       f"handshake complete, protocol {obs.protocol_version}",
                       {"protocol_version": obs.protocol_version,
                        "server": obs.server_name})
    return Verdict("initialize_conformance", BREACH,
                   "initialized without declaring a protocol version", {})


def advertises_tools(obs: Observation, cfg: CertConfig) -> Verdict:
    if not obs.reachable:
        return Verdict("advertises_tools", UNKNOWN, "server unreachable", {})
    n = len(obs.tools)
    if n >= 1:
        return Verdict("advertises_tools", PASS, f"{n} tools advertised",
                       {"tool_count": n})
    return Verdict("advertises_tools", BREACH, "tools/list returned no tools", {})


def tool_schemas_wellformed(obs: Observation, cfg: CertConfig) -> Verdict:
    if not obs.reachable or not obs.tools:
        return Verdict("tool_schemas_wellformed", UNKNOWN,
                       "no tool catalog to inspect", {})
    bad = [t.name for t in obs.tools if not _schema_wellformed(t.input_schema)]
    if not bad:
        return Verdict("tool_schemas_wellformed", PASS,
                       f"all {len(obs.tools)} tools declare a valid object schema",
                       {"tool_count": len(obs.tools)})
    return Verdict("tool_schemas_wellformed", BREACH,
                   f"{len(bad)} tool(s) declare no usable input schema",
                   {"offenders": bad[:20]})


def result_contract(obs: Observation, cfg: CertConfig) -> Verdict:
    reads = [p for p in obs.probes if p.label.startswith("read:")]
    done = [p for p in reads if p.ok and not p.is_error]
    if not done:
        return Verdict("result_contract", UNKNOWN,
                       "no successful read call to inspect", {})
    empty = [p.tool for p in done if not p.content_types]
    if not empty:
        return Verdict("result_contract", PASS,
                       f"{len(done)} read call(s) returned typed content",
                       {"content_types": sorted({t for p in done for t in p.content_types})})
    return Verdict("result_contract", BREACH,
                   "a read call returned no typed content items",
                   {"offenders": empty})


def bad_args_handled(obs: Observation, cfg: CertConfig) -> Verdict:
    p = obs.probe("bad_args")
    if p is None:
        return Verdict("bad_args_handled", UNKNOWN,
                       "no invalid-argument probe was run", {})
    if p.ok and p.is_error:
        return Verdict("bad_args_handled", PASS,
                       "invalid arguments were rejected with a proper error",
                       {"tool": p.tool, "error_class": p.error_class})
    if not p.ok:
        return Verdict("bad_args_handled", BREACH,
                       "invalid arguments crashed or hung the call (no clean error)",
                       {"tool": p.tool, "error_class": p.error_class, "note": p.note})
    return Verdict("bad_args_handled", BREACH,
                   "invalid arguments were accepted silently (no error reported)",
                   {"tool": p.tool})


def unknown_tool_rejected(obs: Observation, cfg: CertConfig) -> Verdict:
    p = obs.probe("unknown_tool")
    if p is None:
        return Verdict("unknown_tool_rejected", UNKNOWN,
                       "no unknown-tool probe was run", {})
    if p.ok and p.is_error:
        return Verdict("unknown_tool_rejected", PASS,
                       "a call to a nonexistent tool was rejected with a proper error",
                       {"error_class": p.error_class})
    if not p.ok:
        return Verdict("unknown_tool_rejected", BREACH,
                       "a nonexistent tool call crashed or hung the server",
                       {"error_class": p.error_class, "note": p.note})
    return Verdict("unknown_tool_rejected", BREACH,
                   "a nonexistent tool returned a success (should have errored)", {})


def latency_slo(obs: Observation, cfg: CertConfig) -> Verdict:
    lat = obs.read_latencies()
    if len(lat) < MIN_LATENCY_SAMPLES:
        return Verdict("latency_slo", UNKNOWN,
                       f"only {len(lat)} read sample(s), need {MIN_LATENCY_SAMPLES}",
                       {"samples": len(lat)})
    p95 = _pctl(lat, 95)
    ev = {"p95_ms": round(p95, 1), "slo_ms": cfg.latency_slo_ms,
          "samples": len(lat), "max_ms": round(max(lat), 1)}
    if p95 <= cfg.latency_slo_ms:
        return Verdict("latency_slo", PASS,
                       f"read p95 {p95:.0f} ms within the {cfg.latency_slo_ms:.0f} ms SLO", ev)
    return Verdict("latency_slo", BREACH,
                   f"read p95 {p95:.0f} ms exceeds the {cfg.latency_slo_ms:.0f} ms SLO", ev)


def catalog_stable(obs: Observation, cfg: CertConfig) -> Verdict:
    if not obs.reachable or not obs.tools:
        return Verdict("catalog_stable", UNKNOWN, "no tool catalog to compare", {})
    if cfg.baseline_tools is None:
        return Verdict("catalog_stable", UNKNOWN,
                       "no pinned baseline yet (run with --pin-baseline to set one)",
                       {"fingerprint": catalog_fingerprint(obs.tools)})
    now = catalog_fingerprint(obs.tools)
    base = catalog_fingerprint(cfg.baseline_tools)
    if now == base:
        return Verdict("catalog_stable", PASS,
                       "tool contract matches the pinned baseline",
                       {"fingerprint": now})
    diff = catalog_diff(cfg.baseline_tools, obs.tools)
    return Verdict("catalog_stable", BREACH,
                   "tool contract drifted from the pinned baseline", diff)


# Registry order = severity order (handshake first). Each entry is (id, fn).
CONTRACTS = [
    ("initialize_conformance", initialize_conformance),
    ("advertises_tools", advertises_tools),
    ("tool_schemas_wellformed", tool_schemas_wellformed),
    ("result_contract", result_contract),
    ("bad_args_handled", bad_args_handled),
    ("unknown_tool_rejected", unknown_tool_rejected),
    ("latency_slo", latency_slo),
    ("catalog_stable", catalog_stable),
]


@dataclass
class Report:
    """The certified verdict for one MCP server across every contract."""
    url: str
    grade: str
    verdicts: List[Verdict]
    fingerprint: str
    fault_injected: str = ""

    @property
    def counts(self) -> dict:
        c = {PASS: 0, BREACH: 0, UNKNOWN: 0}
        for v in self.verdicts:
            c[v.status] = c.get(v.status, 0) + 1
        return c

    @property
    def breaches(self) -> List[Verdict]:
        return [v for v in self.verdicts if v.status == BREACH]

    def summary_lines(self) -> List[str]:
        c = self.counts
        lines = [f"MCP server {self.url} graded {self.grade} "
                 f"({c[PASS]} pass, {c[BREACH]} breach, {c[UNKNOWN]} unknown)."]
        for v in self.breaches:
            lines.append(f"BREACH {v.contract}: {v.reason}.")
        return lines

    def to_dict(self) -> dict:
        return {"url": self.url, "grade": self.grade,
                "fingerprint": self.fingerprint,
                "fault_injected": self.fault_injected,
                "counts": self.counts,
                "verdicts": [v.to_dict() for v in self.verdicts]}


def grade(verdicts: List[Verdict]) -> str:
    """Fold per-contract verdicts into one honest overall grade.

    FAILED if anything BREACHED. Otherwise CERTIFIED only when every contract was
    actually evaluated (no blind spots); PARTIAL when it passed what it could see
    but some contracts were UNKNOWN; BLIND when nothing was conclusive at all.
    """
    n_pass = sum(v.status == PASS for v in verdicts)
    n_breach = sum(v.status == BREACH for v in verdicts)
    n_unknown = sum(v.status == UNKNOWN for v in verdicts)
    if n_breach:
        return FAILED
    if n_pass and not n_unknown:
        return CERTIFIED
    if n_pass:
        return PARTIAL
    return BLIND


def certify(obs: Observation, cfg: Optional[CertConfig] = None) -> Report:
    """Run every contract against one observation and grade the result."""
    cfg = cfg or CertConfig()
    verdicts = [fn(obs, cfg) for _cid, fn in CONTRACTS]
    fp = catalog_fingerprint(obs.tools) if obs.tools else ""
    return Report(url=obs.url, grade=grade(verdicts), verdicts=verdicts,
                  fingerprint=fp, fault_injected=obs.fault_injected)
