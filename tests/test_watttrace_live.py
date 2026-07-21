"""Live end to end smoke test for WattTrace GreenOps (Track 03).

Unlike the network free unit tests in ``test_energy.py`` and ``test_watttrace.py``,
this one exercises the REAL path: it drives the local agent on Ollama, estimates and
records energy, exports all three signals to the running self hosted SigNoz, and then
queries SigNoz back to prove the run's trace actually landed. It is the automated
version of the manual dry run.

It is deliberately kept OUT of ``tests/run_all.py`` so the default suite stays network
free. Run it on demand, when your stack is up:

    set WATTTRACE_LIVE_SMOKE=1
    python tests/test_watttrace_live.py

Behaviour is fail safe for CI:
  * Without ``WATTTRACE_LIVE_SMOKE=1`` it SKIPS (exit 0), so it never breaks a
    network free pipeline that happens to invoke it.
  * With the flag set but the stack unreachable (Ollama or SigNoz down) it SKIPS
    (exit 0) with a clear message, rather than failing for an environment reason.
  * Only a real regression, a crash, a malformed report, a non fail closed verdict,
    or spans that never reach SigNoz, makes it FAIL (exit 1).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = os.getenv("SIGNOZ_UI", "http://localhost:8080")
OLLAMA = os.getenv("OLLAMA_HOST", "http://localhost:11434")
VALID_STATUSES = {"PASS", "BREACH", "UNKNOWN"}


def _reachable(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def _api_key():
    f = ROOT / ".signoz_api_key"
    return f.read_text().strip() if f.exists() else ""


def _suite_count(key, since_ms):
    """Count watt.suite spans SigNoz has seen since ``since_ms``. Parses the v5
    scalar response for the traces shape (columns[] + data rows)."""
    now = int(time.time() * 1000)
    body = {
        "start": since_ms, "end": now, "requestType": "scalar",
        "compositeQuery": {"queries": [{"type": "builder_query", "spec": {
            "name": "A", "signal": "traces",
            "aggregations": [{"expression": "count()"}],
            "filter": {"expression": "service.name = 'watttrace' AND name = 'watt.suite'"},
            "groupBy": [],
        }}]},
    }
    req = urllib.request.Request(
        f"{BASE}/api/v5/query_range", data=json.dumps(body).encode(), method="POST",
        headers={"SIGNOZ-API-KEY": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        d = json.loads(r.read())
    total = 0.0
    for res in (d.get("data", {}).get("data", {}).get("results") or []):
        # metrics-style scalar, defensive: aggregations[].series[].values
        for a in (res.get("aggregations") or []):
            for s in (a.get("series") or []):
                for v in (s.get("values") or []):
                    try:
                        total += float(v.get("value", 0) or 0)
                    except (TypeError, ValueError):
                        pass
        # traces-style scalar: one aggregation column, one value per group row
        cols = res.get("columns") or []
        agg_idx = [i for i, c in enumerate(cols) if c.get("columnType") == "aggregation"]
        for row in (res.get("data") or []):
            for i in (agg_idx or range(len(row))):
                try:
                    total += float(row[i] or 0)
                except (TypeError, ValueError):
                    pass
    return total


def _skip(msg):
    print(f"SKIP  test_watttrace_live: {msg}")
    return 0


def _fail(msg):
    print(f"FAIL  test_watttrace_live: {msg}")
    return 1


def main():
    if not os.getenv("WATTTRACE_LIVE_SMOKE"):
        return _skip("set WATTTRACE_LIVE_SMOKE=1 to run the live end to end check")
    key = _api_key()
    if not key:
        return _skip(".signoz_api_key missing (cannot authenticate to SigNoz)")
    if not _reachable(f"{OLLAMA}/api/tags"):
        return _skip(f"Ollama not reachable at {OLLAMA}")
    if not _reachable(f"{BASE}/api/v1/health"):
        return _skip(f"SigNoz not reachable at {BASE}")

    tmp = ROOT / "_watt_live_smoke.json"
    since_ms = int(time.time() * 1000) - 5000
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        import watt_report  # heavy imports (agent, telemetry) deferred to here
        print(">>> live smoke: control cohort, 3 questions, exporting to SigNoz ...")
        code = watt_report.run(cohorts="control", questions=3, gate=False,
                               export=True, json_out=str(tmp))
    except Exception as e:  # a crash in the real path is a genuine failure
        return _fail(f"the run raised: {e!r}")

    if code != 0:
        return _fail(f"run returned non-zero exit code {code} without --gate")
    if not tmp.exists():
        return _fail("no JSON report was written")
    report = json.loads(tmp.read_text())

    # 1) The report is well formed and honest.
    checks = []
    checks.append(("service is watttrace", report.get("service") == "watttrace"))
    trace_hex = report.get("trace") or ""
    checks.append(("suite trace id present", len(trace_hex) == 32))
    cohorts = report.get("cohorts") or []
    checks.append(("a cohort was recorded", len(cohorts) >= 1))
    checks.append(("budget is published",
                   isinstance(report.get("budget_joules_per_verified_answer"), (int, float))))
    for c in cohorts:
        checks.append((f"{c.get('name')} verdict is fail closed",
                       c.get("status") in VALID_STATUSES))
        j = c.get("joules")
        checks.append((f"{c.get('name')} energy is non-negative",
                       isinstance(j, (int, float)) and j >= 0))
        checks.append((f"{c.get('name')} wasted energy is non-negative",
                       (c.get("wasted_joules") or 0) >= 0))
    bad = [name for name, ok in checks if not ok]
    if bad:
        try:
            tmp.unlink()
        except OSError:
            pass
        return _fail("report checks failed: " + "; ".join(bad))

    # 2) The round trip: the run's spans actually reached SigNoz. Poll for the
    #    watt.suite span to appear (a few seconds of ingestion lag is normal).
    seen = 0.0
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            seen = _suite_count(key, since_ms)
        except urllib.error.HTTPError as e:
            return _fail(f"SigNoz query_range returned HTTP error: {e.read()[:160]!r}")
        except Exception as e:
            return _fail(f"SigNoz query_range failed: {e!r}")
        if seen >= 1:
            break
        time.sleep(5)

    try:
        tmp.unlink()
    except OSError:
        pass

    if seen < 1:
        return _fail("run completed but no watt.suite span reached SigNoz within 60s "
                     "(export or ingestion path is broken)")

    print(f"PASS  test_watttrace_live: {len(checks)} report checks, "
          f"trace {trace_hex} visible in SigNoz "
          f"({', '.join(c['name'] + '=' + c['status'] for c in cohorts)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
