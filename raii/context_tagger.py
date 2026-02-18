"""
Tags tool results (context chunks) with the currently active task at ingestion time.
Persists ContextChunk records to SQLite.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Set

from .storage import get_conn, serialize, deserialize
from .task_registry import TaskRegistry

# Tools whose results can be re-fetched on demand — safe to mark refetchable
REFETCHABLE_TOOLS = frozenset({"Read", "Glob", "Grep", "WebFetch", "WebSearch"})

# Rough token estimation: 1 token ≈ 4 characters
_CHARS_PER_TOKEN = 4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


@dataclass
class ContextChunk:
    id: str                          # tool_use_id from Claude Code
    tool_name: str
    tool_input: dict = field(default_factory=dict)
    task_ids: Set[str] = field(default_factory=set)
    is_refetchable: bool = False
    status: str = "fresh"            # fresh | integrated | evictable
    size_tokens: int = 0
    created_at: str = field(default_factory=_now)
    session_id: Optional[str] = None
    content_hash: Optional[str] = None


class ContextTagger:
    """
    Ingests tool results, creates ContextChunk records, and associates them
    with the currently active task via TaskRegistry.
    """

    def __init__(self, registry: Optional[TaskRegistry] = None):
        self._registry = registry or TaskRegistry()

    def ingest(
        self,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict,
        tool_output: str,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> ContextChunk:
        """
        Create and persist a ContextChunk for a completed tool call.

        If task_id is not provided, the currently active task (from registry) is used.
        """
        active_task_id = task_id or self._get_active_task_id()

        chunk = ContextChunk(
            id=tool_use_id,
            tool_name=tool_name,
            tool_input=tool_input,
            task_ids={active_task_id} if active_task_id else set(),
            is_refetchable=tool_name in REFETCHABLE_TOOLS,
            status="fresh",
            size_tokens=_estimate_tokens(tool_output),
            session_id=session_id,
            content_hash=_content_hash(tool_output),
        )

        self._persist(chunk)

        if active_task_id:
            self._registry.tag_chunk(active_task_id, tool_use_id)

        return chunk

    def get(self, chunk_id: str) -> Optional[ContextChunk]:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM context_chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_chunk(conn, row)

    def mark_evictable(self, chunk_id: str) -> None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE context_chunks SET status = 'evictable' WHERE id = ?",
                (chunk_id,),
            )

    def mark_integrated(self, chunk_id: str) -> None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE context_chunks SET status = 'integrated' WHERE id = ?",
                (chunk_id,),
            )

    def list_evictable(self) -> list[ContextChunk]:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM context_chunks WHERE status = 'evictable'"
            ).fetchall()
            return [self._row_to_chunk(conn, r) for r in rows]

    def list_all(self) -> list[ContextChunk]:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM context_chunks ORDER BY created_at"
            ).fetchall()
            return [self._row_to_chunk(conn, r) for r in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_active_task_id(self) -> Optional[str]:
        task = self._registry.get_current_active()
        return task.id if task else None

    def _persist(self, chunk: ContextChunk) -> None:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO context_chunks
                    (id, tool_name, tool_input, is_refetchable, status,
                     size_tokens, created_at, session_id, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status       = excluded.status,
                    size_tokens  = excluded.size_tokens,
                    content_hash = excluded.content_hash
                """,
                (
                    chunk.id,
                    chunk.tool_name,
                    serialize(chunk.tool_input),
                    int(chunk.is_refetchable),
                    chunk.status,
                    chunk.size_tokens,
                    chunk.created_at,
                    chunk.session_id,
                    chunk.content_hash,
                ),
            )

    def _row_to_chunk(self, conn, row) -> ContextChunk:
        task_rows = conn.execute(
            "SELECT task_id FROM task_chunks WHERE chunk_id = ?", (row["id"],)
        ).fetchall()
        return ContextChunk(
            id=row["id"],
            tool_name=row["tool_name"],
            tool_input=deserialize(row["tool_input"]),
            task_ids={r["task_id"] for r in task_rows},
            is_refetchable=bool(row["is_refetchable"]),
            status=row["status"],
            size_tokens=row["size_tokens"],
            created_at=row["created_at"],
            session_id=row["session_id"],
            content_hash=row["content_hash"],
        )
