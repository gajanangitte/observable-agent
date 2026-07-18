"""One canary rollout of the managed workload.

The healer launches this as a subprocess with env derived from the control
plane, so config.py picks up the current model + fault configuration fresh (a
real rollout, not an in-process monkey-patch). Every span it emits is tagged
``experiment.id=<cohort>`` so the healer can score *this* rollout in SigNoz,
cleanly isolated from every other rollout and from the healer's own telemetry.
"""
import os
import time

import telemetry
from agent import Agent

# A small, fixed workload -- enough to produce a stable retry-rate signal while
# keeping each rollout to a couple of minutes on CPU.
QUESTIONS = [
    "Is the checkout service healthy? If not, what should I do?",
    "What's the error-budget burn for checkout if our SLO is 99.9%?",
    "Give me a one-line status for the payments service.",
]


def main():
    n = int(os.getenv("CANARY_QUESTIONS", str(len(QUESTIONS))))
    cohort = os.getenv("EXPERIMENT_ID", "?")
    telemetry.setup_telemetry()
    agent = Agent()
    for i, q in enumerate(QUESTIONS[:n], 1):
        t0 = time.perf_counter()
        try:
            agent.invoke(q)
            print(f"  canary[{cohort}] {i}/{n}  {(time.perf_counter() - t0):5.1f}s  ok")
        except Exception as e:  # noqa: BLE001
            print(f"  canary[{cohort}] {i}/{n}  ERROR {e}")
    telemetry.shutdown()


if __name__ == "__main__":
    main()
