#!/usr/bin/env python3
"""
Measures per-invocation latency of pre_tool_use and post_tool_use hooks.
Run from the repo root:
    python3 benchmarks/bench_hook_latency.py
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from statistics import mean, median, stdev

REPO = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

TOOLS = [
    ("Read",   {"file_path": "/Users/sanjitrameshkumar/.zshrc"},  "file content " * 200),
    ("Bash",   {"command": "ls"},                                  "file1\nfile2\nfile3\n"),
    ("Grep",   {"pattern": "def ", "path": "."},                   "match1\nmatch2\n"),
]


def run_hook(script: str, event: dict) -> float:
    start = time.perf_counter()
    proc = subprocess.run(
        [PYTHON, str(REPO / "hooks" / script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - start
    if proc.returncode != 0:
        print(f"  STDERR: {proc.stderr[:200]}", file=sys.stderr)
    return elapsed


def bench(hook_script: str, events: list[dict], n: int = 50) -> dict:
    times = []
    for _ in range(n):
        for ev in events:
            times.append(run_hook(hook_script, ev))
    return {
        "n": len(times),
        "mean_ms": mean(times) * 1000,
        "median_ms": median(times) * 1000,
        "stdev_ms": stdev(times) * 1000,
        "p95_ms": sorted(times)[int(len(times) * 0.95)] * 1000,
        "max_ms": max(times) * 1000,
    }


def main():
    uid = "tu-bench-001"
    pre_events = [
        {
            "session_id": "bench",
            "tool_name": name,
            "tool_use_id": uid,
            "tool_input": inp,
        }
        for name, inp, _ in TOOLS
    ]
    post_events = [
        {
            "session_id": "bench",
            "tool_name": name,
            "tool_use_id": uid,
            "tool_input": inp,
            "tool_result": result,
        }
        for name, inp, result in TOOLS
    ]

    print(f"Warming up...")
    for ev in pre_events:
        run_hook("pre_tool_use.py", ev)
    for ev in post_events:
        run_hook("post_tool_use.py", ev)

    print(f"\nBenchmarking pre_tool_use ({len(pre_events)*50} calls)...")
    pre = bench("pre_tool_use.py", pre_events)
    print(f"  mean={pre['mean_ms']:.1f}ms  median={pre['median_ms']:.1f}ms  "
          f"p95={pre['p95_ms']:.1f}ms  max={pre['max_ms']:.1f}ms")

    print(f"\nBenchmarking post_tool_use ({len(post_events)*50} calls)...")
    post = bench("post_tool_use.py", post_events)
    print(f"  mean={post['mean_ms']:.1f}ms  median={post['median_ms']:.1f}ms  "
          f"p95={post['p95_ms']:.1f}ms  max={post['max_ms']:.1f}ms")

    total_overhead_per_call = pre["mean_ms"] + post["mean_ms"]
    print(f"\nEstimated overhead per tool call: {total_overhead_per_call:.1f}ms")
    print(f"(Claude Code tool calls typically take 500ms–30s, so <50ms overhead is fine)")


if __name__ == "__main__":
    main()
