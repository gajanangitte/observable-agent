"""Robust-statistics anomaly detection for the healer's senses.

A fixed SLO floor (``retry_rate > 5%``) is a guarantee, but it is blind to a
regression that stays *under* the floor yet is wildly out of character for the
service, and it needs a human to pick the number. This module supplements the
fixed floor with distribution-free statistics computed over the agent's own
recent, SigNoz-derived history:

  * ``robust_z``  -- a median/MAD modified z-score. Unlike mean/stddev it is not
                     dragged around by the very outliers it is meant to catch.
  * ``ewma``      -- an exponentially weighted moving average (the smoothed
                     baseline the current value is judged against).
  * ``cusum``     -- a cumulative-sum change detector that flags a small but
                     *persistent* upward shift a single-point z-score would miss.

Everything here is pure, deterministic, and dependency-free (stdlib ``math``
only), so it is trivially unit-tested on synthetic series and adds nothing to the
judges' reproduction footprint. There is deliberately NO neural net: an SRE has
to be able to read *why* a remediation fired, and "value X is 6.2 robust-sigma
above a baseline of Y" is auditable in a way an autoencoder's reconstruction
error is not.
"""
import math

# Modified z-score above which a single point is treated as an outlier. 3.5 is
# the conventional Iglewicz-Hoaglin cut-off for the median/MAD z-score.
DEFAULT_Z_THRESHOLD = 3.5


def median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def mad(xs, med=None):
    """Median absolute deviation: the robust analogue of standard deviation."""
    if not xs:
        return 0.0
    m = median(xs) if med is None else med
    return median([abs(x - m) for x in xs])


def _mean_std(xs):
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    mean = sum(xs) / n
    var = sum((v - mean) ** 2 for v in xs) / n
    return mean, math.sqrt(var)


def robust_z(baseline, x):
    """Modified z-score of ``x`` against a ``baseline`` series.

    Uses median/MAD (0.6745 = the normal-consistency constant). When the MAD is
    degenerate (a perfectly flat baseline) it falls back to mean/stddev so the
    score is still defined. Returns 0.0 when the baseline is too small to judge.
    """
    if len(baseline) < 3:
        return 0.0
    m = median(baseline)
    d = mad(baseline, m)
    if d > 0:
        return 0.6745 * (x - m) / d
    mean, sd = _mean_std(baseline)
    if sd == 0:
        return 0.0
    return (x - mean) / sd


def ewma(xs, alpha=0.3):
    """Exponentially weighted moving average of a series (recent-weighted)."""
    if not xs:
        return 0.0
    e = xs[0]
    for v in xs[1:]:
        e = alpha * v + (1 - alpha) * e
    return e


def cusum(xs, target=None, k=0.5, h=4.0):
    """One-sided upper CUSUM. Detects a persistent *upward* shift.

    ``k`` (slack) and ``h`` (alarm threshold) are expressed in robust-sigma units
    (scaled by the series MAD) so the same constants work whatever the metric's
    magnitude. Returns ``(alarm, peak_statistic)``.
    """
    if len(xs) < 3:
        return (False, 0.0)
    mu = median(xs) if target is None else target
    scale = mad(xs)
    if scale <= 0:
        _, sd = _mean_std(xs)
        scale = sd
    if scale <= 0:
        return (False, 0.0)
    s = 0.0
    peak = 0.0
    for v in xs:
        s = max(0.0, s + (v - mu) - k * scale)
        peak = max(peak, s)
    return (peak > h * scale, peak)


def assess(baseline, x, z_threshold=DEFAULT_Z_THRESHOLD):
    """Combine the single-point outlier test and the persistent-shift test.

    ``baseline`` is the recent healthy history; ``x`` is the current reading.
    Returns a small dict the sensor stamps onto its span:

        baseline   -- the median of the recent history (what "normal" looks like)
        sigma      -- how many robust-sigma the current value sits above baseline
        anomaly    -- True if EITHER test fires (outlier OR persistent shift)
        z_outlier  -- the single-point median/MAD test fired
        cusum_alarm-- the cumulative-sum persistent-shift test fired
        n          -- baseline sample count (anomaly needs n >= 3 to mean anything)
    """
    base = list(baseline)
    z = robust_z(base, x)
    alarm, peak = cusum(base + [x])
    z_outlier = bool(z >= z_threshold) and len(base) >= 3
    return {
        "baseline": round(median(base), 6) if base else 0.0,
        "sigma": round(z, 2),
        "anomaly": bool(z_outlier or alarm),
        "z_outlier": z_outlier,
        "cusum_alarm": bool(alarm),
        "cusum_peak": round(peak, 4),
        "n": len(base),
    }
