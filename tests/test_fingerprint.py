"""Unit tests for heal_fingerprint (deterministic incident identity)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heal_fingerprint as fp


def _retry(rate):
    return {"slo": "retry_tax", "retry_rate": rate, "threshold": 0.05}


def _cost(cpr):
    return {"slo": "cost_runaway", "calls_per_request": cpr, "threshold_calls": 6}


def test_deterministic():
    a = fp.fingerprint(_retry(0.40))
    b = fp.fingerprint(_retry(0.40))
    assert a.class_id == b.class_id
    assert a.full_id == b.full_id


def test_class_id_stable_across_severity():
    mild = fp.fingerprint(_retry(0.08))    # ~1.6x -> low
    severe = fp.fingerprint(_retry(0.90))  # 18x   -> high
    assert mild.severity != severe.severity
    assert mild.class_id == severe.class_id      # same incident CLASS
    assert mild.full_id != severe.full_id        # but distinguishable by severity


def test_different_slo_different_class():
    assert fp.fingerprint(_retry(0.40)).class_id != fp.fingerprint(_cost(20)).class_id


def test_severity_buckets():
    assert fp.severity_bucket(0.04, 0.05) == "none"   # under threshold
    assert fp.severity_bucket(0.08, 0.05) == "low"    # 1.6x
    assert fp.severity_bucket(0.15, 0.05) == "med"    # 3x
    assert fp.severity_bucket(0.50, 0.05) == "high"   # 10x


def test_direction():
    assert fp.fingerprint(_retry(0.40)).direction == "over"
    assert fp.fingerprint(_retry(0.00)).direction == "under"


def test_severity_rank_orders():
    assert fp.severity_rank("low") < fp.severity_rank("high")
    assert fp.severity_rank("bogus") == -1


def test_fault_signature_is_mechanism_not_fix():
    f = fp.fingerprint(_retry(0.40))
    assert f.fault_signature == "retry.reason=response_dropped"
    assert "disable" not in f.fault_signature  # never names a remediation


def test_as_attrs_shape():
    a = fp.fingerprint(_cost(20)).as_attrs()
    assert a["heal.fingerprint.slo"] == "cost_runaway"
    assert a["heal.fingerprint.signature"] == "runaway_llm_loop"
    assert len(a["heal.fingerprint.class"]) == 12


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
