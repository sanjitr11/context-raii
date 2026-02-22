#!/usr/bin/env python3
"""
Benchmark harness: runs all scenarios against isolated SQLite DBs
and reports eviction metrics.

Usage:
    python3 benchmarks/run_harness.py
    python3 benchmarks/run_harness.py --scenario sequential_clean
"""

import argparse
import importlib
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from benchmarks.harness import ScenarioHarness

SCENARIOS = [
    "sequential_clean",
    "cross_cutting_refactor",
    "exploratory_abandon",
    "parallel_tasks",
    "long_chain",
]


def _check(label: str, value: float, lo: float, hi: float) -> bool:
    ok = lo <= value <= hi
    mark = "PASS" if ok else "FAIL"
    expected = f"{lo:.0%}–{hi:.0%}" if hi < 1.0 or lo > 0.0 else f"{lo:.0%}–{hi:.0%}"
    print(f"    [{mark}] {label}: {value:.1%}  (expected {expected})")
    return ok


def run_scenario(name: str) -> dict:
    mod = importlib.import_module(f"benchmarks.scenarios.{name}")

    print(f"\n{'═' * 60}")
    print(f"  {name}")
    print(f"  {mod.DESCRIPTION}")
    print(f"{'═' * 60}")

    with tempfile.TemporaryDirectory() as tmp:
        h = ScenarioHarness(Path(tmp), session_id=f"{name}-session")

        t0 = time.monotonic()
        mod.run(h)
        elapsed = time.monotonic() - t0

        m = h.metrics()

    print(f"\n  Metrics ({elapsed:.1f}s, {m['total_chunks']} chunks, {m['total_tokens']} tokens):")
    print(f"    Eviction rate:        {m['eviction_rate']:.1%}  "
          f"({m['evictable_chunks']}/{m['total_chunks']} chunks, "
          f"{m['evictable_tokens']}/{m['total_tokens']} tokens)")
    print(f"    Task completion:      {m['task_completion_rate']:.1%}  "
          f"({m['completed_tasks']}/{m['total_tasks']} tasks)")
    print(f"    Refetch rate:         {m['refetch_rate']:.1%}  "
          f"({m['refetch_count']} refetch(es))")

    print("\n  Pass/fail:")
    expected = mod.EXPECTED
    results = {}
    all_pass = True

    for metric, (lo, hi) in expected.items():
        ok = _check(metric, m[metric], lo, hi)
        results[metric] = ok
        all_pass = all_pass and ok

    status = "PASS" if all_pass else "FAIL"
    print(f"\n  → {status}")

    return {"name": name, "metrics": m, "pass": all_pass, "elapsed": elapsed}


def main():
    parser = argparse.ArgumentParser(description="Run context-raii scenario benchmarks")
    parser.add_argument(
        "--scenario", "-s",
        choices=SCENARIOS,
        help="Run a single scenario (default: all)",
    )
    args = parser.parse_args()

    to_run = [args.scenario] if args.scenario else SCENARIOS

    results = []
    for name in to_run:
        r = run_scenario(name)
        results.append(r)

    # Summary table
    print(f"\n\n{'═' * 60}")
    print("  SUMMARY")
    print(f"{'═' * 60}")
    print(f"  {'Scenario':<30} {'Eviction':>10} {'Completion':>12} {'Refetch':>9} {'Result':>8}")
    print(f"  {'-'*30} {'-'*10} {'-'*12} {'-'*9} {'-'*8}")
    for r in results:
        m = r["metrics"]
        status = "PASS" if r["pass"] else "FAIL"
        print(
            f"  {r['name']:<30} "
            f"{m['eviction_rate']:>9.1%} "
            f"{m['task_completion_rate']:>11.1%} "
            f"{m['refetch_rate']:>8.1%} "
            f"{status:>8}"
        )
    print()

    failed = [r["name"] for r in results if not r["pass"]]
    if failed:
        print(f"  FAILED: {', '.join(failed)}")
        sys.exit(1)
    else:
        print(f"  All {len(results)} scenario(s) passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
