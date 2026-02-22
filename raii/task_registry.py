"""
Task CRUD and status tracking.
Tasks map to Claude Code's TodoWrite / TaskCreate / TaskUpdate tool calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Set, List, Dict

from .storage import get_conn, serialize, deserialize


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Task:
    id: str
    subject: str
    status: str                          # pending | in_progress | completed
    parent_id: Optional[str] = None
    context_chunk_ids: Set[str] = field(default_factory=set)
    created_at: str = field(default_factory=_now)
    completed_at: Optional[str] = None
    metadata: Dict = field(default_factory=dict)

    def is_active(self) -> bool:
        return self.status in ("pending", "in_progress")

    def is_complete(self) -> bool:
        return self.status == "completed"


class TaskRegistry:
    """
    Persists and retrieves Task objects from SQLite.
    Thread-safe at the SQLite WAL level; one writer at a time is fine for hooks.
    """

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def upsert(self, task: Task) -> None:
        """Insert or replace a task record."""
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO tasks (id, subject, status, parent_id, created_at, completed_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    subject      = excluded.subject,
                    status       = excluded.status,
                    parent_id    = excluded.parent_id,
                    completed_at = excluded.completed_at,
                    metadata     = excluded.metadata
                """,
                (
                    task.id,
                    task.subject,
                    task.status,
                    task.parent_id,
                    task.created_at,
                    task.completed_at,
                    serialize(task.metadata),
                ),
            )

    def create(self, id: str, subject: str, parent_id: Optional[str] = None) -> Task:
        task = Task(id=id, subject=subject, status="pending", parent_id=parent_id)
        self.upsert(task)
        return task

    def update_status(self, task_id: str, status: str) -> Optional[Task]:
        task = self.get(task_id)
        if task is None:
            return None
        task.status = status
        if status == "completed" and task.completed_at is None:
            task.completed_at = _now()
        self.upsert(task)
        return task

    def tag_chunk(self, task_id: str, chunk_id: str) -> None:
        """Associate a context chunk with a task."""
        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO task_chunks (task_id, chunk_id, tagged_at)
                VALUES (?, ?, ?)
                """,
                (task_id, chunk_id, _now()),
            )

    def add_dependency(self, dependent_task_id: str, dependency_task_id: str) -> None:
        """Record that dependent_task builds on dependency_task.
        Chunks owned by dependency_task stay pinned until dependent_task completes."""
        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO task_dependencies (dependent_task_id, dependency_task_id)
                VALUES (?, ?)
                """,
                (dependent_task_id, dependency_task_id),
            )

    def has_active_dependents(self, task_id: str) -> bool:
        """Return True if any task that declared dependsOn this task is still active."""
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM task_dependencies td
                JOIN tasks t ON td.dependent_task_id = t.id
                WHERE td.dependency_task_id = ?
                  AND t.status IN ('pending', 'in_progress')
                """,
                (task_id,),
            ).fetchone()
            return row[0] > 0

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> Optional[Task]:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_task(conn, row)

    def list_active(self) -> List[Task]:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status IN ('pending', 'in_progress')"
            ).fetchall()
            return [self._row_to_task(conn, r) for r in rows]

    def list_all(self) -> List[Task]:
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
            return [self._row_to_task(conn, r) for r in rows]

    def get_current_active(self) -> Optional[Task]:
        """Return the most recently updated in-progress task, if any."""
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE status = 'in_progress' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            return self._row_to_task(conn, row)

    def chunks_for_task(self, task_id: str) -> Set[str]:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT chunk_id FROM task_chunks WHERE task_id = ?", (task_id,)
            ).fetchall()
            return {r["chunk_id"] for r in rows}

    def tasks_for_chunk(self, chunk_id: str) -> List[Task]:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM tasks t
                JOIN task_chunks tc ON t.id = tc.task_id
                WHERE tc.chunk_id = ?
                """,
                (chunk_id,),
            ).fetchall()
            return [self._row_to_task(conn, r) for r in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_to_task(self, conn, row) -> Task:
        chunk_rows = conn.execute(
            "SELECT chunk_id FROM task_chunks WHERE task_id = ?", (row["id"],)
        ).fetchall()
        return Task(
            id=row["id"],
            subject=row["subject"],
            status=row["status"],
            parent_id=row["parent_id"],
            context_chunk_ids={r["chunk_id"] for r in chunk_rows},
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            metadata=deserialize(row["metadata"]),
        )
