"""Tamper-evident audit ledger: a hash-chained record of every heal action.

The policy gate decides whether an action may run, and SigNoz records the trace.
This module adds the third leg of an auditable autonomous system: an **append-only,
tamper-evident ledger** of the governed lifecycle. Each record carries the SHA-256
hash of the record before it (a blockchain-style chain), so any later edit,
deletion, or reordering of a past entry breaks the chain and is detectable with
``verify_chain``. It answers "prove to me nothing was quietly changed after the
fact" -- exactly the evidence an SRE or auditor wants next to an agent that is
allowed to change production config on its own.

Design (matches the codebase's other runtime state):

  * The store is a small JSON file (like ``heal_state.json`` / ``heal_memory.json``),
    written atomically (tmp + ``os.replace``). It starts empty on a fresh clone.
  * Each record is ``{seq, ts, event, data, prev_hash, hash}`` where ``hash`` =
    SHA-256 over the CANONICAL serialisation of everything except ``hash`` itself
    (so the hash covers ``prev_hash``, chaining the records together). The genesis
    record's ``prev_hash`` is 64 zeros.
  * The hash is computed with ``sort_keys`` + compact separators, so it is stable
    regardless of dict ordering -- the same "no drift" discipline as the
    fingerprint module.

Running ``python heal_ledger.py`` verifies the live chain and prints a summary; a
non-zero exit means the ledger was tampered with (a CI-friendly integrity gate).
"""
import hashlib
import json
import os
import time

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heal_ledger.json")
GENESIS_PREV = "0" * 64


class LedgerCorrupt(Exception):
    """The ledger file exists but is not a readable JSON array.

    Raised instead of silently returning an empty chain, so a garbled ledger is
    reported as an integrity failure (never a false ``INTACT``) and ``append``
    refuses to overwrite it -- the audit trail is preserved, not quietly wiped.
    """


def _canonical(record):
    """Deterministic bytes for hashing: every field EXCEPT the record's own hash,
    serialised with sorted keys + compact separators so ordering never matters."""
    payload = {k: v for k, v in record.items() if k != "hash"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(record):
    return hashlib.sha256(_canonical(record)).hexdigest()


def _load(path):
    """Return the chain list. A MISSING file is an empty ledger; a file that EXISTS
    but is corrupt (unreadable or not a JSON array) raises ``LedgerCorrupt`` instead
    of being silently discarded, so a garbled ledger can never read as intact nor be
    quietly overwritten by the next append."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (ValueError, OSError) as exc:
        raise LedgerCorrupt(f"{path} is unreadable or not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise LedgerCorrupt(f"{path} is not a JSON array")
    return data


def _save(path, chain):
    """Atomically replace the ledger (tmp + os.replace). On Windows a concurrent
    reader (e.g. the read-only status console) can briefly hold the target file, so
    the final replace is retried a few times before giving up."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(chain, f, indent=2)
    last = None
    for _ in range(6):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise last


def append(event, data=None, path=PATH, ts=None):
    """Append one tamper-evident record and return it.

    ``event`` is a short lifecycle name (e.g. ``breach.detected``,
    ``action.applied``, ``verify``, ``rollback``, ``outcome``); ``data`` is a small
    JSON-serialisable dict of primitives. The new record is chained to the current
    head and the whole chain is persisted atomically.
    """
    chain = _load(path)
    prev_hash = chain[-1]["hash"] if chain else GENESIS_PREV
    record = {
        "seq": len(chain),
        "ts": time.time() if ts is None else ts,
        "event": event,
        "data": data or {},
        "prev_hash": prev_hash,
    }
    record["hash"] = _hash(record)
    chain.append(record)
    _save(path, chain)
    return record


def verify_chain(path=PATH):
    """Verify the ledger's integrity. Returns ``(ok, problem)``.

    Every record must (a) hash to its stored ``hash``, (b) point its ``prev_hash``
    at the prior record's ``hash`` (genesis points at 64 zeros), and (c) carry a
    contiguous ``seq``. The first failure names the offending record; an empty or
    missing ledger is trivially valid.
    """
    try:
        chain = _load(path)
    except LedgerCorrupt as exc:
        return False, str(exc)
    prev_hash = GENESIS_PREV
    for i, record in enumerate(chain):
        if record.get("seq") != i:
            return False, f"record {i}: seq is {record.get('seq')!r}, expected {i}"
        if record.get("prev_hash") != prev_hash:
            return False, (f"record {i}: prev_hash does not match the prior record's hash "
                           f"(chain broken -- a record was edited, removed, or reordered)")
        if _hash(record) != record.get("hash"):
            return False, f"record {i} ({record.get('event')!r}): contents do not match its hash"
        prev_hash = record["hash"]
    return True, f"{len(chain)} records, chain intact"


def head(path=PATH):
    """The current head record (or None for an empty or unreadable ledger)."""
    try:
        chain = _load(path)
    except LedgerCorrupt:
        return None
    return chain[-1] if chain else None


def records(path=PATH):
    """Every record, or an empty list for a missing or unreadable ledger."""
    try:
        return _load(path)
    except LedgerCorrupt:
        return []


def _main():
    ok, msg = verify_chain()
    try:
        chain = _load(PATH)
    except LedgerCorrupt:
        chain = []
    print(f"heal ledger: {PATH}")
    print(f"  status: {'INTACT' if ok else 'TAMPERED'} -- {msg}")
    if chain:
        h = chain[-1]
        print(f"  head:   seq={h['seq']} event={h['event']!r} hash={h['hash'][:16]}...")
        events = {}
        for r in chain:
            events[r["event"]] = events.get(r["event"], 0) + 1
        print("  events: " + ", ".join(f"{k}={v}" for k, v in sorted(events.items())))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    _main()
