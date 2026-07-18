"""Generate a spread of agent traffic so SigNoz has traces + metrics to explore.

Usage:
    python run_load.py [rounds] [workers]
"""
import concurrent.futures
import sys
import time

import telemetry
from agent import Agent

QUESTIONS = [
    "Is the checkout service healthy? If not, what should I do?",
    "What's the error-budget burn for checkout if our SLO is 99.9%?",
    "Summarize the health of payments and search.",
    "Inventory feels slow. Pull its health, recent deploys, and the right runbook.",
    "Which of my services look degraded right now?",
    "What's the runbook for high latency?",
    "Did checkout deploy anything in the last hour, and could that explain issues?",
    "Compute the error budget for payments at a 99.95% SLO.",
    "Give me a one-line status for the checkout service.",
    "The 'billing' service is paging - what's its health?",  # unknown -> error path
]


def run_one(agent, q, i):
    t0 = time.perf_counter()
    try:
        ans = agent.invoke(q)
        print(f"[{i:02d}] {(time.perf_counter() - t0):5.1f}s  {q}\n       -> {ans[:140].strip()}\n")
    except Exception as e:  # noqa: BLE001
        print(f"[{i:02d}] ERROR  {q} :: {e}")


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    telemetry.setup_telemetry()
    agent = Agent()
    idx = 0
    for _ in range(rounds):
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = []
            for q in QUESTIONS:
                idx += 1
                futs.append(ex.submit(run_one, agent, q, idx))
                time.sleep(0.4)
            for f in futs:
                f.result()
    telemetry.shutdown()
    print(f"done - {idx} agent requests; traces/metrics/logs flushed to SigNoz")


if __name__ == "__main__":
    main()
