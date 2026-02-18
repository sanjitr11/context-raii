#!/usr/bin/env python3
"""
Simulates a realistic Claude Code session by feeding events through the hooks
in sequence, then validates the DB state.

Scenario:
  Task A: read two files, run tests, mark complete
  Task B: read one file (shares a file with task A), still active
  Expected: task A's exclusive chunks → evictable, shared chunk → preserved

Run:
    python3 benchmarks/replay_session.py
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

# Use a fresh temp DB so this doesn't pollute the real state
import os
_tmpdir = Path(tempfile.mkdtemp())
os.environ["RAII_TEST_DB_DIR"] = str(_tmpdir)  # hooks read this if set


def hook(script: str, event: dict) -> dict:
    proc = subprocess.run(
        [PYTHON, str(REPO / "hooks" / script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={**os.environ, "RAII_DB_DIR": str(_tmpdir)},
    )
    if proc.returncode != 0:
        print(f"[{script}] STDERR: {proc.stderr[:300]}")
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


def tool_call(tool_name: str, tool_input: dict, tool_result: str, tool_use_id: str):
    """Simulate one complete tool call (pre + post hook)."""
    event_base = {
        "session_id": "replay-001",
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "tool_input": tool_input,
    }
    hook("pre_tool_use.py", event_base)
    hook("post_tool_use.py", {**event_base, "tool_result": tool_result})


def query_db(sql: str):
    db_path = _tmpdir / "state.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def check(label: str, condition: bool):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return condition


def main():
    print(f"Replay DB: {_tmpdir / 'state.db'}\n")
    all_pass = True

    # ------------------------------------------------------------------
    print("=== Phase 1: Task A starts ===")
    # Create task A
    tool_call("TaskCreate",
              {"id": "task-a", "subject": "Implement feature X"},
              "Task created", "tu-create-a")

    # Task A goes in_progress
    tool_call("TaskUpdate",
              {"taskId": "task-a", "status": "in_progress"},
              "Updated", "tu-update-a1")

    # Task A reads two files
    tool_call("Read",
              {"file_path": "/src/shared.py"},
              "def shared(): pass\n" * 50,   # ~2500 chars
              "tu-read-shared")

    tool_call("Read",
              {"file_path": "/src/feature_x.py"},
              "def feature_x(): pass\n" * 100,  # ~5000 chars
              "tu-read-feature-x")

    # Task A runs tests
    tool_call("Bash",
              {"command": "pytest tests/"},
              "5 passed in 1.2s\n",
              "tu-bash-tests")

    time.sleep(0.05)

    # ------------------------------------------------------------------
    print("\n=== Phase 2: Task B starts (shares shared.py) ===")
    tool_call("TaskCreate",
              {"id": "task-b", "subject": "Refactor shared module"},
              "Task created", "tu-create-b")

    tool_call("TaskUpdate",
              {"taskId": "task-b", "status": "in_progress"},
              "Updated", "tu-update-b1")

    # Task B re-reads the shared file (same path → superseded)
    tool_call("Read",
              {"file_path": "/src/shared.py"},
              "def shared(): pass\n" * 50,
              "tu-read-shared-b")

    # ------------------------------------------------------------------
    print("\n=== Phase 3: Task A completes ===")
    # This should trigger eviction engine
    tool_call("TaskUpdate",
              {"taskId": "task-a", "status": "completed"},
              "Updated", "tu-update-a2")

    time.sleep(0.1)

    # ------------------------------------------------------------------
    print("\n=== Validating DB state ===")

    tasks = query_db("SELECT id, status FROM tasks")
    task_map = {t["id"]: t["status"] for t in tasks}
    all_pass &= check("task-a is completed", task_map.get("task-a") == "completed")
    all_pass &= check("task-b is in_progress", task_map.get("task-b") == "in_progress")

    chunks = query_db("SELECT id, tool_name, status, size_tokens FROM context_chunks")
    chunk_map = {c["id"]: c for c in chunks}

    print(f"\n  Chunks found: {[c['id'] for c in chunks]}")

    # feature_x.py was only used by task A → should be evictable
    all_pass &= check(
        "tu-read-feature-x is evictable (only task A used it)",
        chunk_map.get("tu-read-feature-x", {}).get("status") == "evictable",
    )

    # bash test run was only task A → evictable
    all_pass &= check(
        "tu-bash-tests is evictable (only task A used it)",
        chunk_map.get("tu-bash-tests", {}).get("status") == "evictable",
    )

    # shared.py first read: task A (done) owned it; task B owns the newer read.
    # The first read is superseded by the second, and task A is done → evictable.
    first_shared_status = chunk_map.get("tu-read-shared", {}).get("status")
    all_pass &= check(
        "tu-read-shared (task A's read) is evictable (superseded + task A done)",
        first_shared_status == "evictable",
    )

    # task B's read of shared.py: task B is still active → must be preserved
    b_shared_status = chunk_map.get("tu-read-shared-b", {}).get("status")
    all_pass &= check(
        "tu-read-shared-b (task B's read) is NOT evictable (task B active)",
        b_shared_status != "evictable",
    )

    # Token accounting
    evictable_tokens = sum(
        c["size_tokens"] for c in chunks if c["status"] == "evictable"
    )
    total_tokens = sum(c["size_tokens"] for c in chunks)
    print(f"\n  Token savings: {evictable_tokens}/{total_tokens} tokens evictable "
          f"({100*evictable_tokens//total_tokens if total_tokens else 0}%)")

    # ------------------------------------------------------------------
    print("\n=== Phase 4: PreCompact guidance ===")
    result = hook("pre_compact.py", {
        "session_id": "replay-001",
        "trigger": "auto",
        "context_window_tokens": total_tokens,
    })
    guidance = result.get("additionalContext", "")
    all_pass &= check("PreCompact returns non-empty guidance", len(guidance) > 50)
    all_pass &= check(
        "Guidance mentions safe-to-evict chunks",
        "SAFE TO OMIT" in guidance or "safe" in guidance.lower(),
    )
    print(f"\n  Guidance snippet:\n{guidance[:400]}")

    # ------------------------------------------------------------------
    print(f"\n{'='*50}")
    print(f"Result: {'ALL PASS' if all_pass else 'SOME FAILURES'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
