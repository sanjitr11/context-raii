"""
Dependency edges between context chunks and tasks.
Tracks which tasks reference which chunks, supporting ref-counted eviction decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Set

from .storage import get_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


REFERENCE_TYPES = frozenset(
    {"cited_in_reasoning", "builds_on", "supersedes", "required_by"}
)


@dataclass
class ReferenceEdge:
    source_task_id: str
    target_chunk_id: str
    reference_type: str = "cited_in_reasoning"
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = _now()


class ReferenceGraph:
    """
    Manages the bipartite graph: Tasks ←→ ContextChunks.

    Edges are stored in the `reference_edges` table.
    The `task_chunks` table (managed by TaskRegistry / ContextTagger) tracks
    the primary assignment; this table captures semantic references added later.
    """

    def add_edge(
        self,
        task_id: str,
        chunk_id: str,
        reference_type: str = "cited_in_reasoning",
    ) -> None:
        if reference_type not in REFERENCE_TYPES:
            raise ValueError(f"Unknown reference type: {reference_type!r}")
        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO reference_edges
                    (source_task_id, target_chunk_id, reference_type, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, chunk_id, reference_type, _now()),
            )

    def remove_edge(self, task_id: str, chunk_id: str, reference_type: str) -> None:
        with get_conn() as conn:
            conn.execute(
                """
                DELETE FROM reference_edges
                WHERE source_task_id = ? AND target_chunk_id = ? AND reference_type = ?
                """,
                (task_id, chunk_id, reference_type),
            )

    def chunks_referenced_by_task(self, task_id: str) -> Set[str]:
        """All chunk IDs that have any reference edge from this task."""
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT target_chunk_id FROM reference_edges WHERE source_task_id = ?",
                (task_id,),
            ).fetchall()
            return {r["target_chunk_id"] for r in rows}

    def tasks_referencing_chunk(self, chunk_id: str) -> List[str]:
        """All task IDs that have any reference edge to this chunk."""
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT source_task_id FROM reference_edges WHERE target_chunk_id = ?",
                (chunk_id,),
            ).fetchall()
            return [r["source_task_id"] for r in rows]

    def chunks_referenced_by_active_tasks(self) -> Set[str]:
        """
        Returns chunk IDs referenced by tasks that are still pending or in_progress.
        These chunks must NOT be evicted.
        """
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT re.target_chunk_id
                FROM reference_edges re
                JOIN tasks t ON t.id = re.source_task_id
                WHERE t.status IN ('pending', 'in_progress')
                """
            ).fetchall()
            return {r["target_chunk_id"] for r in rows}

    def all_edges(self) -> List[ReferenceEdge]:
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM reference_edges").fetchall()
            return [
                ReferenceEdge(
                    source_task_id=r["source_task_id"],
                    target_chunk_id=r["target_chunk_id"],
                    reference_type=r["reference_type"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    def edge_count(self) -> int:
        with get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM reference_edges").fetchone()[0]
