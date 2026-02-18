#!/usr/bin/env python3
"""
Reads the live state.db and prints a dashboard of context health.
Run this anytime during or after a real Claude Code session:

    python3 benchmarks/measure_session.py

Or watch it live:
    watch -n 5 python3 benchmarks/measure_session.py
"""

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "raii" / "state.db"
HINTS_PATH = Path.home() / ".claude" / "raii" / "eviction_hints.json"


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def fmt_tokens(n: int) -> str:
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def main():
    if not DB_PATH.exists():
        print("No state.db found. Run a session with hooks active first.")
        return

    conn = connect()

    print(f"\n{'='*60}")
    print(f"  context-raii Dashboard  —  {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*60}")

    # Task breakdown
    tasks = conn.execute("SELECT status, COUNT(*) as n FROM tasks GROUP BY status").fetchall()
    task_counts = {r["status"]: r["n"] for r in tasks}
    total_tasks = sum(task_counts.values())
    print(f"\nTASKS ({total_tasks} total)")
    for status in ("pending", "in_progress", "completed"):
        n = task_counts.get(status, 0)
        bar = "█" * min(n, 30)
        print(f"  {status:<12} {n:>4}  {bar}")

    # Active tasks
    active = conn.execute(
        "SELECT id, subject FROM tasks WHERE status IN ('pending','in_progress') ORDER BY created_at"
    ).fetchall()
    if active:
        print(f"\n  Active:")
        for t in active:
            print(f"    • {t['subject'][:55]}  (id={t['id'][:8]}…)")

    # Chunk breakdown
    chunks = conn.execute(
        "SELECT status, COUNT(*) as n, COALESCE(SUM(size_tokens),0) as tokens "
        "FROM context_chunks GROUP BY status"
    ).fetchall()
    chunk_counts = {r["status"]: (r["n"], r["tokens"]) for r in chunks}
    total_chunks = sum(v[0] for v in chunk_counts.values())
    total_tokens = sum(v[1] for v in chunk_counts.values())

    print(f"\nCONTEXT CHUNKS ({total_chunks} total, {fmt_tokens(total_tokens)} tokens)")
    for status in ("fresh", "integrated", "evictable"):
        n, tokens = chunk_counts.get(status, (0, 0))
        pct = int(100 * tokens / total_tokens) if total_tokens else 0
        bar = "█" * (pct // 3)
        print(f"  {status:<12} {n:>4} chunks  {fmt_tokens(tokens):>6} tokens  {pct:>3}%  {bar}")

    # Savings
    evictable_tokens = chunk_counts.get("evictable", (0, 0))[1]
    if total_tokens > 0:
        savings_pct = int(100 * evictable_tokens / total_tokens)
        print(f"\n  Evictable savings: {fmt_tokens(evictable_tokens)} / {fmt_tokens(total_tokens)} tokens ({savings_pct}%)")

    # Reference graph
    edge_count = conn.execute("SELECT COUNT(*) FROM reference_edges").fetchone()[0]
    active_blocked = conn.execute(
        """
        SELECT COUNT(DISTINCT re.target_chunk_id)
        FROM reference_edges re
        JOIN tasks t ON t.id = re.source_task_id
        WHERE t.status IN ('pending','in_progress')
        """
    ).fetchone()[0]
    print(f"\nREFERENCE GRAPH")
    print(f"  Total edges:         {edge_count}")
    print(f"  Active-blocked chunks: {active_blocked}  (cannot evict)")

    # Tool breakdown of evictable chunks
    evictable_by_tool = conn.execute(
        "SELECT tool_name, COUNT(*) as n, SUM(size_tokens) as tokens "
        "FROM context_chunks WHERE status = 'evictable' GROUP BY tool_name ORDER BY tokens DESC"
    ).fetchall()
    if evictable_by_tool:
        print(f"\nEVICTABLE BY TOOL")
        for r in evictable_by_tool:
            refetch = "✓ re-fetchable" if r["tool_name"] in ("Read","Grep","Glob","WebFetch","WebSearch") else ""
            print(f"  {r['tool_name']:<14} {r['n']:>3} chunks  {fmt_tokens(r['tokens']):>6} tokens  {refetch}")

    # Compaction hints
    if HINTS_PATH.exists():
        hints = json.loads(HINTS_PATH.read_text())
        generated = hints.get("generated_at", "")
        print(f"\nLAST EVICTION HINTS  ({generated[:19]})")
        print(f"  Safe to evict:   {len(hints.get('safe_to_evict', []))} chunks")
        print(f"  Preserved:       {len(hints.get('critical_to_preserve', []))} chunks")
        print(f"  Token savings:   {fmt_tokens(hints.get('token_savings_estimate', 0))}")

    # Compaction avoidance estimate
    # Claude Code typically compacts at ~80k tokens context window usage.
    # How much runway does eviction give us?
    COMPACT_THRESHOLD = 80_000
    saved = evictable_tokens
    if saved > 0:
        runway_pct = int(100 * saved / COMPACT_THRESHOLD)
        print(f"\nCOMPACTION AVOIDANCE ESTIMATE")
        print(f"  Evictable tokens extend context by ~{runway_pct}% of compaction threshold")
        print(f"  ({fmt_tokens(saved)} of ~{fmt_tokens(COMPACT_THRESHOLD)} threshold tokens recoverable)")

    conn.close()
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
