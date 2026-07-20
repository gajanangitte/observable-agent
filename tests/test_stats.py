"""Unit tests for heal_stats (robust anomaly detection). Framework-free: run
directly with ``python tests/test_stats.py`` or under pytest if installed."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heal_stats as s


def test_median_odd_even():
    assert s.median([3, 1, 2]) == 2
    assert s.median([4, 1, 3, 2]) == 2.5
    assert s.median([]) == 0.0


def test_mad_known():
    # deviations from median 3 are [2,1,0,1,2] -> median 1
    assert s.mad([1, 2, 3, 4, 5]) == 1


def test_robust_z_small_baseline_is_zero():
    assert s.robust_z([0.01, 0.0], 5.0) == 0.0  # < 3 samples: undefined -> 0


def test_robust_z_flags_outlier():
    baseline = [0.00, 0.01, 0.00, 0.02, 0.01, 0.00, 0.01, 0.00]
    assert abs(s.robust_z(baseline, 0.01)) < 3.5      # a normal value
    assert s.robust_z(baseline, 0.45) > 3.5           # a clear spike


def test_robust_z_flat_baseline_fallback():
    # perfectly flat baseline -> MAD 0 -> mean/std fallback still defined
    assert s.robust_z([1.0, 1.0, 1.0, 1.0], 1.0) == 0.0


def test_cusum_stable_series_no_alarm():
    stable = [10, 11, 9, 10, 12, 8, 10, 11, 9, 10]
    alarm, _ = s.cusum(stable)
    assert alarm is False


def test_cusum_detects_persistent_shift():
    # ten normal points then a sustained step up -> CUSUM should alarm
    series = [10, 11, 9, 10, 12, 8, 10, 11] + [18, 19, 18, 19, 18]
    alarm, peak = s.cusum(series)
    assert alarm is True
    assert peak > 0


def test_assess_healthy_value_is_not_anomaly():
    baseline = [0.00, 0.01, 0.00, 0.02, 0.01, 0.00, 0.01]
    v = s.assess(baseline, 0.0)
    assert v["anomaly"] is False
    assert v["n"] == len(baseline)


def test_assess_spike_is_anomaly():
    baseline = [0.00, 0.01, 0.00, 0.02, 0.01, 0.00, 0.01]
    v = s.assess(baseline, 0.40)
    assert v["anomaly"] is True
    assert v["sigma"] > 3.5


def test_assess_cold_start_never_anomaly():
    # with < 3 samples the supplement stays silent (the fixed floor still guards)
    assert s.assess([], 99.0)["anomaly"] is False
    assert s.assess([0.0, 0.0], 99.0)["anomaly"] is False


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
