#!/usr/bin/env python3
"""
ScenarioHarness: drives Claude Code hooks against an isolated SQLite DB
and computes eviction metrics.

Usage:
    from benchmarks.harness import ScenarioHarness
    with tempfile.TemporaryDirectory() as tmp:
        h = ScenarioHarness(Path(tmp))
        h.task_create("t1", "Do something")
        h.task_update("t1", "in_progress")
        h.read_file("/src/foo.py")
        h.task_update("t1", "completed")
        print(h.metrics())
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class ScenarioHarness:
    def __init__(self, tmp_dir: Path, session_id: str = "scenario-session"):
        self.tmp_dir = Path(tmp_dir)
        self.session_id = session_id
        self._call_count = 0
        self._env = {**os.environ, "RAII_DB_DIR": str(self.tmp_dir)}

    # ------------------------------------------------------------------
    # Hook execution
    # ------------------------------------------------------------------

    def _hook(self, script: str, event: dict) -> dict:
        proc = subprocess.run(
            [PYTHON, str(REPO / "hooks" / script)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            env=self._env,
        )
        out = proc.stdout.strip()
        return json.loads(out) if out else {}

    def tool_call(
        self,
        tool_name: str,
        tool_input: dict,
        tool_response=None,
        tool_use_id: Optional[str] = None,
    ) -> dict:
        """Fire pre_tool_use then post_tool_use for a single tool call."""
        self._call_count += 1
        tid = tool_use_id or f"tu-{self._call_count:04d}"
        base = {
            "session_id": self.session_id,
            "tool_name": tool_name,
            "tool_use_id": tid,
            "tool_input": tool_input,
        }
        pre = self._hook("pre_tool_use.py", base)
        if pre.get("decision") == "block":
            return {"blocked": True, "reason": pre.get("reason", "")}

        response = tool_response if tool_response is not None else {"text": "ok"}
        self._hook("post_tool_use.py", {**base, "tool_response": response})
        return {"blocked": False}

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def task_create(self, task_id: str, subject: str, depends_on: list = None):
        self.tool_call(
            "TaskCreate",
            {"id": task_id, "subject": subject, "dependsOn": depends_on or []},
            {"text": f"Task created: {subject}"},
        )

    def task_update(self, task_id: str, status: str):
        self.tool_call(
            "TaskUpdate",
            {"taskId": task_id, "status": status},
            {"text": f"Updated task #{task_id} status"},
        )

    def read_file(self, file_path: str, content: str = None):
        c = content or ("x" * 400)  # ~100 tokens
        self.tool_call(
            "Read",
            {"file_path": file_path},
            {"file": {"filePath": file_path, "content": c}},
        )

    def edit_file(self, file_path: str, old_string: str = "old", new_string: str = "new content"):
        self.tool_call(
            "Edit",
            {"file_path": file_path, "old_string": old_string, "new_string": new_string},
            {"filePath": file_path, "newString": new_string},
        )

    def bash(self, command: str, output: str = ""):
        self.tool_call(
            "Bash",
            {"command": command},
            {"stdout": output, "stderr": ""},
        )

    def grep(self, pattern: str, path: str = ".", output: str = "match"):
        self.tool_call(
            "Grep",
            {"pattern": pattern, "path": path},
            {"type": "text", "text": output},
        )

    def pre_compact(self):
        """Fire the PreCompact hook to run the eviction engine and generate hints."""
        return self._hook("pre_compact.py", {
            "session_id": self.session_id,
            "trigger": "auto",
            "context_window_tokens": 50000,
        })

    # ------------------------------------------------------------------
    # DB access
    # ------------------------------------------------------------------

    def query_db(self, sql: str, params=()) -> list:
        db_path = self.tmp_dir / "state.db"
        if not db_path.exists():
            return []
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def chunk_status(self, chunk_id: str) -> Optional[str]:
        rows = self.query_db(
            "SELECT status FROM context_chunks WHERE id = ?", (chunk_id,)
        )
        return rows[0]["status"] if rows else None

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def metrics(self) -> dict:
        chunks = self.query_db(
            "SELECT id, tool_name, status, size_tokens, tool_input, "
            "created_at, status_changed_at FROM context_chunks"
        )
        tasks = self.query_db("SELECT id, status FROM tasks")

        total = len(chunks)
        evictable = sum(1 for c in chunks if c["status"] == "evictable")
        total_tokens = sum(c["size_tokens"] for c in chunks)
        evictable_tokens = sum(
            c["size_tokens"] for c in chunks if c["status"] == "evictable"
        )

        total_tasks = len(tasks)
        completed_tasks = sum(1 for t in tasks if t["status"] == "completed")
        abandoned_tasks = sum(1 for t in tasks if t["status"] == "abandoned")

        refetches = self._count_refetches(chunks)

        return {
            "total_chunks": total,
            "evictable_chunks": evictable,
            "eviction_rate": evictable / total if total else 0,
            "total_tokens": total_tokens,
            "evictable_tokens": evictable_tokens,
            "token_eviction_rate": evictable_tokens / total_tokens if total_tokens else 0,
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "abandoned_tasks": abandoned_tasks,
            "task_completion_rate": completed_tasks / total_tasks if total_tasks else 0,
            "refetch_count": refetches,
            "refetch_rate": refetches / max(evictable, 1),
        }

    def _count_refetches(self, chunks: list) -> int:
        """
        Proxy for false eviction: count Read chunks where the same file was
        re-read AFTER that chunk was marked evictable. This indicates the
        eviction was premature — Claude had to re-fetch content it lost.
        """
        evicted_reads = [
            c for c in chunks
            if c["tool_name"] == "Read"
            and c["status"] == "evictable"
            and c.get("status_changed_at")
        ]
        all_reads = [c for c in chunks if c["tool_name"] == "Read"]

        count = 0
        for evicted in evicted_reads:
            try:
                evicted_path = json.loads(evicted["tool_input"]).get("file_path")
                evicted_at = evicted["status_changed_at"]
                for later in all_reads:
                    if later["id"] == evicted["id"]:
                        continue
                    later_path = json.loads(later["tool_input"]).get("file_path")
                    if later_path == evicted_path and later["created_at"] > evicted_at:
                        count += 1
                        break
            except Exception:
                pass
        return count
