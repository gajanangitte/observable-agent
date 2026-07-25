"""Unit tests for heal_grounding (the independent evidence gate / confidence tier).

Pure and network-free: the auditor is a deterministic function of the decision
context, so these exercise every tier, the hard gate, and both caps directly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heal_grounding as g


def test_full_evidence_llm_is_high():
    v = g.audit(slo="retry_tax", action="disable_fault_injection", decider="llm",
                evidence_read=True, fingerprint_known=True, hard_floor=True)
    assert v.tier == g.TIER_HIGH
    assert v.grounded is True
    assert v.evidence_read and v.action_supported and v.fingerprint_known
    assert v.score >= 0.85


def test_verified_replay_counts_as_evidence():
    # A memory replay did not call read_incident this run, but it is a fix SigNoz
    # already verified -- grounded by construction, so still HIGH.
    v = g.audit(slo="retry_tax", action="enable_mitigation", decider="memory",
                evidence_read=False, fingerprint_known=True, hard_floor=True)
    assert v.tier == g.TIER_HIGH
    assert v.evidence_read is True   # verified replay is treated as evidence


def test_no_evidence_read_drops_confidence():
    # Same known action + fingerprint, but the model never read the incident.
    with_ev = g.audit(slo="retry_tax", action="disable_fault_injection", decider="llm",
                      evidence_read=True, fingerprint_known=True, hard_floor=True)
    without = g.audit(slo="retry_tax", action="disable_fault_injection", decider="llm",
                      evidence_read=False, fingerprint_known=True, hard_floor=True)
    assert without.score < with_ev.score
    assert g._TIER_RANK[without.tier] < g._TIER_RANK[with_ev.tier]
    assert any("did NOT consult" in r for r in without.reasons)


def test_unsupported_action_is_hard_none():
    # set_cost_budget does not address a retry_tax fault -> NONE regardless of the
    # rest of the context (the one hard gate).
    v = g.audit(slo="retry_tax", action="set_cost_budget", decider="llm",
                evidence_read=True, fingerprint_known=True, hard_floor=True)
    assert v.tier == g.TIER_NONE
    assert v.grounded is False
    assert v.action_supported is False


def test_cost_action_matches_cost_slo():
    v = g.audit(slo="cost_runaway", action="set_cost_budget", decider="llm",
                evidence_read=True, fingerprint_known=True, hard_floor=True)
    assert v.action_supported is True
    assert v.tier == g.TIER_HIGH


def test_no_action_is_none():
    v = g.audit(slo="retry_tax", action="", decider="llm", evidence_read=True,
                fingerprint_known=True, hard_floor=True)
    assert v.tier == g.TIER_NONE
    assert v.grounded is False


def test_anomaly_only_capped_at_medium():
    # Everything supported, but it was an anomaly-only signal -> never above MEDIUM.
    v = g.audit(slo="retry_tax", action="disable_fault_injection", decider="llm",
                evidence_read=True, fingerprint_known=True, hard_floor=False)
    assert v.tier == g.TIER_MEDIUM
    assert v.hard_floor is False
    assert any("anomaly" in r for r in v.reasons)


def test_unknown_slo_capped_at_low():
    # An unrecognised SLO: support cannot be verified -> never above LOW even with
    # evidence + a known fingerprint.
    v = g.audit(slo="mystery_slo", action="do_something", decider="llm",
                evidence_read=True, fingerprint_known=True, hard_floor=True)
    assert v.tier == g.TIER_LOW
    assert v.action_supported is False


def test_carbon_slo_shares_retry_remediations():
    v = g.audit(slo="carbon_slo", action="enable_mitigation", decider="memory",
                evidence_read=False, fingerprint_known=True, hard_floor=True)
    assert v.action_supported is True
    assert v.tier == g.TIER_HIGH


def test_action_suffix_is_ignored():
    v = g.audit(slo="cost_runaway", action="switch_model:llama3.2:1b", decider="llm",
                evidence_read=True, fingerprint_known=True, hard_floor=True)
    assert v.action_supported is True


def test_annotate_is_span_safe():
    v = g.audit(slo="retry_tax", action="disable_fault_injection", decider="llm",
                evidence_read=True, fingerprint_known=True, hard_floor=True)
    v.annotate(None)   # must not raise on a missing span

    class FakeSpan:
        def __init__(self):
            self.attrs = {}

        def set_attribute(self, k, val):
            self.attrs[k] = val

    sp = FakeSpan()
    v.annotate(sp)
    assert sp.attrs["heal.grounding.tier"] == g.TIER_HIGH
    assert sp.attrs["heal.grounding.grounded"] is True
    assert "heal.grounding.reason" in sp.attrs


def test_line_is_readable():
    v = g.audit(slo="retry_tax", action="disable_fault_injection", decider="llm",
                evidence_read=True, fingerprint_known=True, hard_floor=True)
    line = v.line()
    assert line.startswith("[GROUNDING:HIGH]")
    assert "score=" in line


def test_tiers_are_monotonic_in_support():
    # Strictly more support never produces a lower tier.
    none_ev = g.audit(slo="retry_tax", action="disable_fault_injection", decider="llm",
                      evidence_read=False, fingerprint_known=False, hard_floor=True)
    fp_only = g.audit(slo="retry_tax", action="disable_fault_injection", decider="llm",
                      evidence_read=False, fingerprint_known=True, hard_floor=True)
    full = g.audit(slo="retry_tax", action="disable_fault_injection", decider="llm",
                   evidence_read=True, fingerprint_known=True, hard_floor=True)
    ranks = [g._TIER_RANK[x.tier] for x in (none_ev, fp_only, full)]
    assert ranks == sorted(ranks)
