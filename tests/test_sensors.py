"""Offline tests for the three-state sensor logic, driven by a fake MCP client so
they need neither SigNoz nor the network. Locks in the critical safety property:
a blind sensor reports UNKNOWN, never a silent healthy 0."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heal_sensors as hs

# Keep the real baseline file untouched: stub the store in-memory for the test.
hs.heal_baseline.series = lambda slo: []
hs.heal_baseline.append = lambda slo, v: None


class FakeMCP:
    """Returns signoz_aggregate_traces-shaped JSON, or raises to simulate an
    outage. ``pick`` maps a query's filter string to the scalar to return (or
    None to simulate a query that ran but matched no rows)."""

    def __init__(self, pick, fail=False):
        self.url = "http://fake/mcp"
        self._pick = pick
        self._fail = fail

    def call_tool(self, name, args):
        if self._fail:
            raise RuntimeError("MCP down")
        v = self._pick(args.get("filter", ""))
        rows = [] if v is None else [[0, v]]
        return json.dumps({"data": {"data": {"results": [{"data": rows}]}}})


def _retry_pick(total, dropped):
    def pick(f):
        return dropped if "retry.reason" in f else total
    return pick


def test_retry_hard_breach():
    slo = hs.retry_slo(FakeMCP(_retry_pick(10, 4)), "c")
    assert slo["status"] == hs.STATUS_BREACH
    assert slo["hard_breach"] is True
    assert slo["breached"] is True
    assert slo["retry_rate"] == 0.4
    assert slo["fingerprint"]["slo"] == "retry_tax"


def test_retry_pass():
    slo = hs.retry_slo(FakeMCP(_retry_pick(10, 0)), "c")
    assert slo["status"] == hs.STATUS_PASS
    assert slo["breached"] is False
    assert slo["known"] is True


def test_retry_unknown_when_no_calls():
    # zero calls == nothing to judge -> UNKNOWN (retryable), NOT a healthy 0
    slo = hs.retry_slo(FakeMCP(_retry_pick(0, 0)), "c")
    assert slo["status"] == hs.STATUS_UNKNOWN
    assert slo["retryable"] is True
    assert slo["breached"] is False


def test_retry_unknown_when_mcp_down():
    # the fail-open bug regression guard: MCP down must be UNKNOWN, never healthy
    slo = hs.retry_slo(FakeMCP(_retry_pick(10, 4), fail=True), "c")
    assert slo["status"] == hs.STATUS_UNKNOWN
    assert slo["retryable"] is False
    assert slo["breached"] is False
    assert slo["known"] is False


def test_cost_breach_and_unknown():
    def pick_breach(f):
        return 20 if "llm.chat" in f else 2      # 20 calls / 2 reqs = 10/req
    slo = hs.cost_slo(FakeMCP(pick_breach), "c")
    assert slo["status"] == hs.STATUS_BREACH
    assert slo["calls_per_request"] == 10.0

    slo2 = hs.cost_slo(FakeMCP(lambda f: 5 if "llm.chat" in f else 0), "c")  # 0 requests
    assert slo2["status"] == hs.STATUS_UNKNOWN
    assert slo2["retryable"] is True


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
