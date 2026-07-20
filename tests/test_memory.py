"""Unit tests for heal_memory: verified recall, reinforcement, severity gating.
Uses a throwaway store path so the real heal_memory.json is never touched."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heal_fingerprint as fpm
import heal_memory as mem

mem.PATH = os.path.join(tempfile.gettempdir(), "heal_memory_test.json")


def _reset():
    mem._save({})


def _fp(rate):
    return fpm.fingerprint({"slo": "retry_tax", "retry_rate": rate, "threshold": 0.05})


def test_record_then_recall_hit():
    _reset()
    fp = _fp(0.40)
    mem.record_success(fp, "disable_fault_injection", 1500, "traceA")
    hit = mem.recall(fp, allowed=["disable_fault_injection", "enable_mitigation"])
    assert hit is not None
    assert hit["action_base"] == "disable_fault_injection"
    assert hit["verified"] is True


def test_recall_miss_on_unrelated_class():
    _reset()
    mem.record_success(_fp(0.40), "disable_fault_injection", 1500, "traceA")
    cost_fp = fpm.fingerprint({"slo": "cost_runaway", "calls_per_request": 20, "threshold_calls": 6})
    assert mem.recall(cost_fp, allowed=["set_cost_budget"]) is None


def test_recall_respects_allowed_actions():
    _reset()
    mem.record_success(_fp(0.40), "disable_fault_injection", 1500, "traceA")
    # the remembered action is not offered this time -> no recall
    assert mem.recall(_fp(0.40), allowed=["enable_mitigation"]) is None


def test_severity_gate_blocks_worse_incident():
    _reset()
    mild = _fp(0.08)     # low severity
    severe = _fp(0.90)   # high severity, same class
    assert mild.class_id == severe.class_id
    mem.record_success(mild, "disable_fault_injection", 1000, "traceLow")
    # proven only on 'low'; a 'high' recurrence must NOT blindly replay
    assert mem.recall(severe, allowed=["disable_fault_injection"]) is None
    # but a fix proven on 'high' is safe to replay on a milder recurrence
    _reset()
    mem.record_success(severe, "disable_fault_injection", 1000, "traceHigh")
    assert mem.recall(mild, allowed=["disable_fault_injection"]) is not None


def test_reinforcement_counts_and_best_mttr():
    _reset()
    fp = _fp(0.40)
    mem.record_success(fp, "disable_fault_injection", 2000, "t1")
    rec = mem.record_success(fp, "disable_fault_injection", 1200, "t2")
    assert rec["count"] == 2
    assert rec["mttr_ms_best"] == 1200
    assert rec["mttr_ms_last"] == 1200


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
