"""Run every unit test in this directory with no external dependencies.

    python tests/run_all.py

Each test module is plain asserts (also pytest-compatible). These cover the
deterministic core -- fingerprinting, robust stats, the three-state sensor logic,
the policy + parameter gate, and verified memory -- so they need neither SigNoz,
Ollama, nor the network.
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
