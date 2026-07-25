"""Unit tests for heal_ledger (the tamper-evident hash-chained audit ledger).

Network-free and isolated: every test writes to a temp ledger path, so the real
heal_ledger.json is never touched. They prove the chain links, that verification
catches every class of tampering (edit, delete, reorder, hash forgery), and that
the hash is stable regardless of dict key ordering.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heal_ledger as L


def _tmp():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)   # start with no file, like a fresh clone
    return path


def test_empty_ledger_is_valid():
    path = _tmp()
    ok, msg = L.verify_chain(path)
    assert ok is True
    assert L.head(path) is None
    assert L.records(path) == []


def test_append_chains_and_verifies():
    path = _tmp()
    try:
        r0 = L.append("breach.detected", {"slo": "retry_tax", "value": "40%"}, path=path)
        r1 = L.append("action.applied", {"action": "disable_fault_injection"}, path=path)
        r2 = L.append("outcome", {"healed": True, "mttr_s": 41}, path=path)
        assert r0["seq"] == 0 and r1["seq"] == 1 and r2["seq"] == 2
        assert r0["prev_hash"] == L.GENESIS_PREV
        assert r1["prev_hash"] == r0["hash"]
        assert r2["prev_hash"] == r1["hash"]
        ok, msg = L.verify_chain(path)
        assert ok is True
        assert "3 records" in msg
        assert L.head(path)["event"] == "outcome"
    finally:
        os.remove(path)


def test_detects_edited_record():
    path = _tmp()
    try:
        L.append("breach.detected", {"slo": "retry_tax"}, path=path)
        L.append("outcome", {"healed": True}, path=path)
        # Tamper: change a past record's data WITHOUT recomputing hashes.
        chain = json.load(open(path))
        chain[0]["data"]["slo"] = "cost_runaway"
        json.dump(chain, open(path, "w"))
        ok, msg = L.verify_chain(path)
        assert ok is False
        assert "record 0" in msg
    finally:
        os.remove(path)


def test_detects_deleted_record():
    path = _tmp()
    try:
        L.append("a", {}, path=path)
        L.append("b", {}, path=path)
        L.append("c", {}, path=path)
        chain = json.load(open(path))
        del chain[1]   # remove the middle record
        json.dump(chain, open(path, "w"))
        ok, msg = L.verify_chain(path)
        assert ok is False
    finally:
        os.remove(path)


def test_detects_reordered_records():
    path = _tmp()
    try:
        L.append("a", {}, path=path)
        L.append("b", {}, path=path)
        chain = json.load(open(path))
        chain[0], chain[1] = chain[1], chain[0]
        json.dump(chain, open(path, "w"))
        ok, msg = L.verify_chain(path)
        assert ok is False
    finally:
        os.remove(path)


def test_detects_forged_hash():
    path = _tmp()
    try:
        L.append("a", {"x": 1}, path=path)
        chain = json.load(open(path))
        # Change data AND naively overwrite the stored hash with a wrong value: the
        # recomputed hash still will not match, so forgery is caught.
        chain[0]["data"]["x"] = 999
        chain[0]["hash"] = "deadbeef" * 8
        json.dump(chain, open(path, "w"))
        ok, msg = L.verify_chain(path)
        assert ok is False
    finally:
        os.remove(path)


def test_hash_is_order_independent():
    # The canonical serialisation sorts keys, so two records with the same content
    # in different insertion order hash identically.
    a = {"seq": 0, "ts": 1.0, "event": "e", "data": {"b": 2, "a": 1}, "prev_hash": L.GENESIS_PREV}
    b = {"prev_hash": L.GENESIS_PREV, "data": {"a": 1, "b": 2}, "event": "e", "ts": 1.0, "seq": 0}
    assert L._hash(a) == L._hash(b)


def test_hash_covers_prev_hash():
    # Two records identical except for prev_hash must hash differently (the chain
    # link is part of what is signed).
    a = {"seq": 1, "ts": 1.0, "event": "e", "data": {}, "prev_hash": "a" * 64}
    b = {"seq": 1, "ts": 1.0, "event": "e", "data": {}, "prev_hash": "b" * 64}
    assert L._hash(a) != L._hash(b)


def test_deterministic_ts_makes_hash_reproducible():
    # With an explicit ts, appending the same event twice to two fresh ledgers
    # yields identical genesis hashes -- fully deterministic.
    p1, p2 = _tmp(), _tmp()
    try:
        r1 = L.append("x", {"k": "v"}, path=p1, ts=123.0)
        r2 = L.append("x", {"k": "v"}, path=p2, ts=123.0)
        assert r1["hash"] == r2["hash"]
    finally:
        for p in (p1, p2):
            if os.path.exists(p):
                os.remove(p)


def test_corrupt_ledger_is_not_reported_intact():
    # A garbled ledger must read as an integrity FAILURE, never a false INTACT, and
    # readers must degrade gracefully instead of crashing.
    path = _tmp()
    try:
        with open(path, "w") as f:
            f.write("{ this is not valid json")
        ok, msg = L.verify_chain(path)
        assert ok is False
        assert L.head(path) is None
        assert L.records(path) == []
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_append_refuses_to_overwrite_corrupt_ledger():
    # append must NOT silently wipe a ledger it cannot parse; it raises so the
    # existing (possibly recoverable) file is preserved for an operator.
    path = _tmp()
    try:
        with open(path, "w") as f:
            f.write("garbage not json")
        raised = False
        try:
            L.append("outcome", {"healed": True}, path=path)
        except L.LedgerCorrupt:
            raised = True
        assert raised is True
        assert open(path).read() == "garbage not json"
    finally:
        if os.path.exists(path):
            os.remove(path)
