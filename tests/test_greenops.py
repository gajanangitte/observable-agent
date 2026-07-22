"""Offline tests for the GreenOps cross-track sensor: ``heal_sensors.carbon_slo``
prices a cohort's SigNoz spans with the WattTrace energy model (Track 03) and folds
the wasted retry energy into the healer's three-state SLO logic (Track 01). Driven by
a fake MCP client, so they need neither SigNoz, Ollama, nor the network.

The breach signal is the share of a cohort's inference energy wasted on
dropped-and-retried calls (the retry tax, priced in joules), thresholded at the same
5 percent the retry SLO uses. That is calibration-free: a healthy cohort wastes zero
energy and always passes, so a verified fix can never be rolled back by token noise.
The fail-closed property mirrors the WattTrace verdict: answers served with no recorded
token energy is a broken meter -> UNKNOWN, never a free green PASS."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heal_sensors as hs
import energy

# Keep the real baseline file untouched: stub the store in-memory for the test, so
# a fresh carbon_slo series starts empty (no anomaly) and PASS values are not saved.
hs.heal_baseline.series = lambda slo: []
hs.heal_baseline.append = lambda slo, v: None


class FakeMCP:
    """Returns signoz_aggregate_traces-shaped JSON. Dispatches on the query so the
    answer count (agent.invoke), the total token sums, and the dropped-call token
    sums (filter carries retry.reason) are each distinct, split by aggregateOn for
    input vs output. ``fail`` simulates an MCP outage; a None token value simulates a
    query that ran but matched no rows."""

    def __init__(self, answers, in_tok, out_tok, drop_in=0, drop_out=0, fail=False,
                 fail_dropped=False):
        self.url = "http://fake/mcp"
        self._answers = answers
        self._in, self._out = in_tok, out_tok
        self._din, self._dout = drop_in, drop_out
        self._fail = fail
        self._fail_dropped = fail_dropped

    def call_tool(self, name, args):
        if self._fail:
            raise RuntimeError("MCP down")
        f = args.get("filter", "")
        # Simulate ONLY the dropped-and-retried energy query failing (the totals read
        # fine). This is the fail-open trap: a broken numerator read must not be scored
        # as zero waste and handed back as a false PASS.
        if self._fail_dropped and "retry.reason" in f:
            raise RuntimeError("MCP down (dropped-call query)")
        on = args.get("aggregateOn", "")
        if "agent.invoke" in f:
            v = self._answers
        elif "retry.reason" in f:                 # dropped-and-retried calls only
            v = self._din if on == "gen_ai.usage.input_tokens" else self._dout
        elif on == "gen_ai.usage.input_tokens":   # all llm.chat calls
            v = self._in
        elif on == "gen_ai.usage.output_tokens":
            v = self._out
        else:
            v = 0
        rows = [] if v is None else [[0, v]]
        return json.dumps({"data": {"data": {"results": [{"data": rows}]}}})


def test_carbon_breach_wasted_retry_energy():
    # A cohort that wasted ~26 percent of its inference energy on dropped-and-retried
    # calls is a sustainability breach (the retry tax, priced in joules).
    mcp = FakeMCP(answers=3, in_tok=3000, out_tok=300, drop_in=1200, drop_out=60)
    slo = hs.carbon_slo(mcp, "c")
    assert slo["status"] == hs.STATUS_BREACH
    assert slo["hard_breach"] is True
    assert slo["breached"] is True
    assert slo["known"] is True
    assert slo["wasted_fraction"] > hs.RETRY_SLO_MAX_RATE
    assert slo["wasted_joules_per_answer"] > 0
    assert slo["fingerprint"]["slo"] == "carbon_slo"
    assert slo["fingerprint"]["direction"] == "over"


def test_carbon_pass_no_waste():
    # A clean cohort (no dropped calls) wastes zero energy and passes, even though it
    # still burns real joules serving its answers.
    slo = hs.carbon_slo(FakeMCP(answers=5, in_tok=1500, out_tok=300), "c")
    assert slo["status"] == hs.STATUS_PASS
    assert slo["breached"] is False
    assert slo["known"] is True
    assert slo["wasted_fraction"] == 0.0
    assert slo["joules_per_answer"] > 0


def test_carbon_pass_heavy_but_clean():
    # Design property: the SLO gates on WASTED energy, not an absolute per-answer cap,
    # so a legitimately heavy but retry-free workload is NOT falsely breached. This is
    # what makes the heal's verify robust against run-to-run token variance.
    slo = hs.carbon_slo(FakeMCP(answers=1, in_tok=6000, out_tok=1500), "c")
    assert slo["status"] == hs.STATUS_PASS
    assert slo["wasted_fraction"] == 0.0
    assert slo["over_ref_budget"] is True          # footprint is over the WattTrace ref
    assert slo["breached"] is False                # but there is no waste to heal


def test_carbon_below_threshold_passes():
    # A trickle of waste under the 5 percent floor is not a breach (matches retry SLO).
    mcp = FakeMCP(answers=4, in_tok=4000, out_tok=800, drop_in=60, drop_out=10)
    slo = hs.carbon_slo(mcp, "c")
    assert slo["wasted_fraction"] < hs.RETRY_SLO_MAX_RATE
    assert slo["status"] == hs.STATUS_PASS


def test_carbon_unknown_on_zero_energy():
    # Answers served but zero tokens == a broken meter. Must be UNKNOWN (retryable),
    # NEVER a comforting green PASS. This is the fail-closed zero guard, cross-track.
    slo = hs.carbon_slo(FakeMCP(answers=3, in_tok=0, out_tok=0), "c")
    assert slo["status"] == hs.STATUS_UNKNOWN
    assert slo["retryable"] is True
    assert slo["breached"] is False


def test_carbon_unknown_when_no_answers():
    # No agent.invoke answers ingested yet == nothing to judge -> UNKNOWN (retryable).
    slo = hs.carbon_slo(FakeMCP(answers=0, in_tok=100, out_tok=20), "c")
    assert slo["status"] == hs.STATUS_UNKNOWN
    assert slo["retryable"] is True


def test_carbon_unknown_when_mcp_down():
    # Blind sensor (MCP down) is UNKNOWN and NOT retryable, never a silent healthy 0.
    slo = hs.carbon_slo(FakeMCP(answers=3, in_tok=1000, out_tok=200, fail=True), "c")
    assert slo["status"] == hs.STATUS_UNKNOWN
    assert slo["retryable"] is False
    assert slo["known"] is False


def test_carbon_model_matches_energy_module():
    # The sensor's totals must be exactly what the WattTrace model computes for the
    # same token counts (no drift between the heal SLO and the Track 03 verdict).
    est = energy.estimate(input_tokens=1600, output_tokens=200)
    slo = hs.carbon_slo(FakeMCP(answers=4, in_tok=1600, out_tok=200), "c")
    assert abs(slo["joules"] - round(float(est.joules), 1)) < 0.5
    assert abs(slo["joules_per_answer"] - float(est.joules) / 4) < 0.5


def test_carbon_unknown_when_dropped_query_fails():
    # Fail-closed on the BREACH NUMERATOR too: if the totals read fine but the
    # dropped-and-retried energy query ERRORS, the sensor must refuse (UNKNOWN,
    # not retryable), NEVER read the failure as zero waste and return a false PASS
    # that would then verify-heal a still-broken cohort. (Regression: the dropped
    # reads previously used ``_sum(...) or 0.0``, which swallowed a failed query.)
    mcp = FakeMCP(answers=3, in_tok=3000, out_tok=300, drop_in=1200, drop_out=60,
                  fail_dropped=True)
    slo = hs.carbon_slo(mcp, "c")
    assert slo["status"] == hs.STATUS_UNKNOWN
    assert slo["known"] is False
    assert slo["retryable"] is False
    assert slo["breached"] is False


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
