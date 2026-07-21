"""Unit tests for the MCP Contract Lab pure core (network-free).

These lock in the certification judgement without an MCP server, SigNoz, or the
network: every test hand-builds an :class:`Observation` (the raw facts a probe would
have recorded) and asserts the deterministic three-state verdict each contract
returns, the drift fingerprint / diff, and the way per-contract verdicts fold into
one overall grade. This is the safety net for the "observability as tests" core: it
proves a blind check is UNKNOWN (never a silent PASS) and that a single injected
fault flips exactly one contract.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp2_model import (
    PASS, BREACH, UNKNOWN, CERTIFIED, PARTIAL, FAILED, BLIND,
    ToolInfo, CallProbe, Observation, Verdict,
    catalog_fingerprint, catalog_diff,
)
import mcp2_contracts as C
from mcp2_contracts import CertConfig, certify, grade


# --- observation builders ----------------------------------------------------
def _tools(n=3):
    return [ToolInfo(name=f"srv_list_{i}", input_schema={"type": "object", "properties": {}})
            for i in range(n)]


def _clean_obs(tools=None, latencies=(120.0, 140.0, 160.0)):
    """A fully healthy observation: reachable, tools well-formed, clean reads,
    proper bad-arg + unknown-tool errors, reads within the latency SLO."""
    tools = _tools() if tools is None else tools
    probes = []
    for i, ms in enumerate(latencies):
        probes.append(CallProbe(label=f"read:{i}", tool=f"srv_list_{i}", ok=True,
                                is_error=False, latency_ms=ms, content_types=["text"]))
    probes.append(CallProbe(label="bad_args", tool="srv_aggregate", ok=True,
                            is_error=True, error_class="tool_error"))
    probes.append(CallProbe(label="unknown_tool", tool="does_not_exist", ok=True,
                            is_error=True, error_class="protocol"))
    return Observation(url="http://x/mcp", reachable=True,
                       protocol_version="2025-11-25", server_name="TestMCP",
                       tools=tools, probes=probes)


def _cfg(obs=None):
    """A CertConfig whose baseline IS the clean catalog, so catalog_stable PASSes."""
    tools = obs.tools if obs is not None else _tools()
    return CertConfig(baseline_tools=[ToolInfo(name=t.name, input_schema=t.input_schema)
                                      for t in tools])


# --- initialize_conformance --------------------------------------------------
def test_initialize_pass():
    v = C.initialize_conformance(_clean_obs(), _cfg())
    assert v.status == PASS and "2025-11-25" in v.reason


def test_initialize_unknown_when_unreachable():
    obs = Observation(url="http://x/mcp", reachable=False)
    assert C.initialize_conformance(obs, _cfg()).status == UNKNOWN


def test_initialize_breach_without_protocol():
    obs = Observation(url="http://x/mcp", reachable=True, protocol_version=None)
    assert C.initialize_conformance(obs, _cfg()).status == BREACH


# --- advertises_tools --------------------------------------------------------
def test_advertises_tools_pass():
    assert C.advertises_tools(_clean_obs(), _cfg()).status == PASS


def test_advertises_tools_breach_when_empty():
    obs = _clean_obs(tools=[])
    assert C.advertises_tools(obs, _cfg()).status == BREACH


def test_advertises_tools_unknown_when_unreachable():
    obs = Observation(url="http://x/mcp", reachable=False)
    assert C.advertises_tools(obs, _cfg()).status == UNKNOWN


# --- tool_schemas_wellformed -------------------------------------------------
def test_schemas_pass_on_object_and_properties():
    tools = [ToolInfo("a", {"type": "object"}),
             ToolInfo("b", {"properties": {"x": {"type": "string"}}})]
    obs = _clean_obs(tools=tools)
    assert C.tool_schemas_wellformed(obs, _cfg(obs)).status == PASS


def test_schemas_breach_on_missing_schema():
    tools = [ToolInfo("a", {"type": "object"}), ToolInfo("b", None)]
    obs = _clean_obs(tools=tools)
    v = C.tool_schemas_wellformed(obs, _cfg(obs))
    assert v.status == BREACH and "b" in v.evidence["offenders"]


def test_schemas_unknown_without_catalog():
    obs = Observation(url="http://x/mcp", reachable=True, tools=[])
    assert C.tool_schemas_wellformed(obs, _cfg()).status == UNKNOWN


def test_schema_wellformed_helper():
    assert C._schema_wellformed({"type": "object"})
    assert C._schema_wellformed({"properties": {}})
    assert not C._schema_wellformed(None)
    assert not C._schema_wellformed({"type": "string"})
    assert not C._schema_wellformed([1, 2])


# --- result_contract ---------------------------------------------------------
def test_result_contract_pass():
    assert C.result_contract(_clean_obs(), _cfg()).status == PASS


def test_result_contract_breach_on_empty_content():
    """The corrupt fault: a read returns ok but no typed content items."""
    obs = _clean_obs()
    for p in obs.probes:
        if p.label.startswith("read:"):
            p.content_types = []
    v = C.result_contract(obs, _cfg())
    assert v.status == BREACH and v.evidence["offenders"]


def test_result_contract_unknown_without_clean_read():
    obs = _clean_obs()
    obs.probes = [p for p in obs.probes if not p.label.startswith("read:")]
    assert C.result_contract(obs, _cfg()).status == UNKNOWN


def test_result_contract_ignores_errored_reads():
    """A read that errored is not a sample of the result contract at all."""
    obs = _clean_obs(latencies=())
    obs.probes.insert(0, CallProbe(label="read:0", tool="srv", ok=True,
                                   is_error=True, content_types=[]))
    assert C.result_contract(obs, _cfg()).status == UNKNOWN


# --- bad_args_handled --------------------------------------------------------
def test_bad_args_pass_on_proper_error():
    assert C.bad_args_handled(_clean_obs(), _cfg()).status == PASS


def test_bad_args_breach_on_crash():
    obs = _clean_obs()
    obs.probe("bad_args").ok = False
    obs.probe("bad_args").is_error = False
    assert C.bad_args_handled(obs, _cfg()).status == BREACH


def test_bad_args_breach_on_silent_accept():
    obs = _clean_obs()
    obs.probe("bad_args").ok = True
    obs.probe("bad_args").is_error = False
    v = C.bad_args_handled(obs, _cfg())
    assert v.status == BREACH and "silently" in v.reason


def test_bad_args_unknown_when_not_probed():
    obs = _clean_obs()
    obs.probes = [p for p in obs.probes if p.label != "bad_args"]
    assert C.bad_args_handled(obs, _cfg()).status == UNKNOWN


# --- unknown_tool_rejected ---------------------------------------------------
def test_unknown_tool_pass():
    assert C.unknown_tool_rejected(_clean_obs(), _cfg()).status == PASS


def test_unknown_tool_breach_on_fake_success():
    obs = _clean_obs()
    obs.probe("unknown_tool").is_error = False
    assert C.unknown_tool_rejected(obs, _cfg()).status == BREACH


def test_unknown_tool_breach_on_crash():
    obs = _clean_obs()
    obs.probe("unknown_tool").ok = False
    obs.probe("unknown_tool").is_error = False
    assert C.unknown_tool_rejected(obs, _cfg()).status == BREACH


def test_unknown_tool_unknown_when_not_probed():
    obs = _clean_obs()
    obs.probes = [p for p in obs.probes if p.label != "unknown_tool"]
    assert C.unknown_tool_rejected(obs, _cfg()).status == UNKNOWN


# --- latency_slo -------------------------------------------------------------
def test_latency_pass_within_slo():
    assert C.latency_slo(_clean_obs(), _cfg()).status == PASS


def test_latency_breach_over_slo():
    obs = _clean_obs(latencies=(9000.0, 9500.0, 12000.0))
    v = C.latency_slo(obs, _cfg())
    assert v.status == BREACH and v.evidence["p95_ms"] > 8000


def test_latency_unknown_too_few_samples():
    obs = _clean_obs(latencies=(120.0,))
    assert C.latency_slo(obs, _cfg()).status == UNKNOWN


def test_latency_custom_slo():
    obs = _clean_obs(latencies=(200.0, 300.0, 400.0))
    cfg = CertConfig(latency_slo_ms=100.0, baseline_tools=obs.tools)
    assert C.latency_slo(obs, cfg).status == BREACH


def test_pctl_nearest_rank():
    assert C._pctl([], 95) == 0.0
    assert C._pctl([10.0], 95) == 10.0
    assert C._pctl([1.0, 2.0, 3.0, 4.0], 95) == 4.0


# --- catalog_stable + drift fingerprint --------------------------------------
def test_catalog_stable_pass_on_match():
    obs = _clean_obs()
    assert C.catalog_stable(obs, _cfg(obs)).status == PASS


def test_catalog_stable_unknown_without_baseline():
    obs = _clean_obs()
    cfg = CertConfig(baseline_tools=None)
    v = C.catalog_stable(obs, cfg)
    assert v.status == UNKNOWN and "fingerprint" in v.evidence


def test_catalog_stable_breach_on_drift():
    obs = _clean_obs()
    base = [ToolInfo(name=t.name, input_schema=t.input_schema) for t in obs.tools]
    obs.tools.append(ToolInfo(name="srv_new_tool", input_schema={"type": "object"}))
    v = C.catalog_stable(obs, CertConfig(baseline_tools=base))
    assert v.status == BREACH and "srv_new_tool" in v.evidence["added"]


def test_fingerprint_stable_and_order_independent():
    a = [ToolInfo("x", {"type": "object"}), ToolInfo("y", {"type": "object"})]
    b = [ToolInfo("y", {"type": "object"}), ToolInfo("x", {"type": "object"})]
    assert catalog_fingerprint(a) == catalog_fingerprint(b)


def test_fingerprint_changes_on_schema_change():
    a = [ToolInfo("x", {"type": "object"})]
    b = [ToolInfo("x", {"type": "object", "properties": {"q": {"type": "string"}}})]
    assert catalog_fingerprint(a) != catalog_fingerprint(b)


def test_catalog_diff_added_removed_changed():
    base = [ToolInfo("keep", {"type": "object"}),
            ToolInfo("gone", {"type": "object"}),
            ToolInfo("morph", {"type": "object"})]
    cur = [ToolInfo("keep", {"type": "object"}),
           ToolInfo("fresh", {"type": "object"}),
           ToolInfo("morph", {"type": "object", "properties": {"a": {}}})]
    d = catalog_diff(base, cur)
    assert d["added"] == ["fresh"]
    assert d["removed"] == ["gone"]
    assert d["changed"] == ["morph"]


# --- grade folding -----------------------------------------------------------
def test_grade_certified_when_all_known_pass():
    vs = [Verdict("a", PASS, ""), Verdict("b", PASS, "")]
    assert grade(vs) == CERTIFIED


def test_grade_failed_on_any_breach():
    vs = [Verdict("a", PASS, ""), Verdict("b", BREACH, ""), Verdict("c", UNKNOWN, "")]
    assert grade(vs) == FAILED


def test_grade_partial_when_pass_with_unknowns():
    vs = [Verdict("a", PASS, ""), Verdict("b", UNKNOWN, "")]
    assert grade(vs) == PARTIAL


def test_grade_blind_when_nothing_conclusive():
    vs = [Verdict("a", UNKNOWN, ""), Verdict("b", UNKNOWN, "")]
    assert grade(vs) == BLIND


# --- certify() end to end (still network-free) --------------------------------
def test_certify_clean_is_certified():
    obs = _clean_obs()
    rep = certify(obs, _cfg(obs))
    assert rep.grade == CERTIFIED
    assert rep.counts[BREACH] == 0
    assert rep.counts[UNKNOWN] == 0
    assert len(rep.verdicts) == len(C.CONTRACTS)


def test_certify_corrupt_flips_only_result_contract():
    """The single-fault property: corrupt content BREACHes result_contract and
    nothing else, so the red cell points straight at the defect."""
    obs = _clean_obs()
    for p in obs.probes:
        if p.label.startswith("read:"):
            p.content_types = []
    rep = certify(obs, _cfg(obs))
    assert rep.grade == FAILED
    assert [v.contract for v in rep.breaches] == ["result_contract"]


def test_certify_latency_fault_flips_only_latency():
    obs = _clean_obs(latencies=(9000.0, 9500.0, 12000.0))
    rep = certify(obs, _cfg(obs))
    assert rep.grade == FAILED
    assert [v.contract for v in rep.breaches] == ["latency_slo"]


def test_report_counts_and_summary():
    obs = _clean_obs()
    obs.probe("unknown_tool").is_error = False  # inject one breach
    rep = certify(obs, _cfg(obs))
    c = rep.counts
    assert c[PASS] + c[BREACH] + c[UNKNOWN] == len(C.CONTRACTS)
    lines = rep.summary_lines()
    assert any("BREACH unknown_tool_rejected" in ln for ln in lines)
    d = rep.to_dict()
    assert d["grade"] == FAILED and "verdicts" in d


# --- Observation helpers -----------------------------------------------------
def test_observation_roundtrip():
    obs = _clean_obs()
    back = Observation.from_dict(obs.to_dict())
    assert back.url == obs.url
    assert len(back.tools) == len(obs.tools)
    assert len(back.probes) == len(obs.probes)
    assert catalog_fingerprint(back.tools) == catalog_fingerprint(obs.tools)


def test_read_latencies_only_clean_reads():
    obs = _clean_obs(latencies=(100.0, 200.0))
    obs.probes.append(CallProbe(label="read:bad", tool="t", ok=True, is_error=True,
                                latency_ms=999.0, content_types=[]))
    assert sorted(obs.read_latencies()) == [100.0, 200.0]


def test_verdict_properties():
    assert Verdict("a", BREACH, "").breached
    assert Verdict("a", PASS, "").known
    assert not Verdict("a", UNKNOWN, "").known
