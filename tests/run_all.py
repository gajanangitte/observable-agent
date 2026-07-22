"""Run every unit test in this directory with no external dependencies.

    python tests/run_all.py

Each test module is plain asserts (also pytest-compatible). Together they cover the
deterministic core -- fingerprinting, robust stats, the three-state sensor logic,
the policy + parameter gate, and verified memory -- plus the reliability surfaces
around it: tiered model routing and cost math (test_config), the plug-and-play
economics model with real pricing and cost-of-downtime data (test_economics), the
plug-and-play WattTrace energy model with its deterministic token-basis physics,
fail-closed GreenOps verdict and estimate-provenance honesty (test_energy), the
SLO-aligned alert specs and their single-source-of-truth thresholds (test_alert),
the SigNoz->heal
bridge's alert routing / cooldown keying / incident span-link reader (test_bridge),
and the telemetry plumbing itself -- the incident span-link handoff and the
trace-correlated structured heal logs, checked with in-memory OTel exporters
(test_telemetry) -- the MCP Contract Lab's pure certification core: the eight
three-state reliability contracts, the drift fingerprint / diff, and the grade
folding, all on hand-built observations (test_mcp2_contracts) -- and the WattTrace
runner's fail-closed answer verifier plus its energy accumulator (test_watttrace),
and the cross-track GreenOps finale -- the carbon / energy SLO sensor that grades a
cohort with the WattTrace model and folds it into the healer's three-state logic
(test_greenops). They need neither SigNoz, Ollama, nor the network. The MCP client
facade's text extraction and its isError fail-closed raise are covered too, with a
stubbed transport (test_mcp_client).
"""
import importlib
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODULES = [
    "tests.test_stats",
    "tests.test_fingerprint",
    "tests.test_policy",
    "tests.test_sensors",
    "tests.test_actuators",
    "tests.test_memory",
    "tests.test_config",
    "tests.test_economics",
    "tests.test_energy",
    "tests.test_alert",
    "tests.test_bridge",
    "tests.test_telemetry",
    "tests.test_mcp2_contracts",
    "tests.test_watttrace",
    "tests.test_greenops",
    "tests.test_mcp_client",
]


def main():
    total = 0
    failed = 0
    for name in MODULES:
        mod = importlib.import_module(name)
        fns = [v for k, v in sorted(vars(mod).items()) if k.startswith("test_")]
        for fn in fns:
            total += 1
            try:
                fn()
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}.{fn.__name__}")
                traceback.print_exc()
        print(f"  {name}: {len(fns)} tests")
    print(f"\n{'ALL PASSED' if not failed else str(failed) + ' FAILED'}  "
          f"({total - failed}/{total})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
