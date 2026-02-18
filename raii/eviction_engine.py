"""
Determines which context chunks are safe to evict.

Eviction rules:
1. All tasks in chunk.task_ids have status = completed
2. No in-progress or pending task has a ReferenceEdge pointing to this chunk
3. OR the chunk has been superseded (a newer call to the same tool with the same args exists)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import List, Set, Dict, Optional

from .context_tagger import ContextChunk, ContextTagger
from .reference_graph import ReferenceGraph
from .task_registry import TaskRegistry
from .storage import get_conn

log = logging.getLogger(__name__)


@dataclass
class EvictionReport:
    evictable_chunks: List[ContextChunk] = field(default_factory=list)
    preserved_chunks: List[ContextChunk] = field(default_factory=list)
    total_evictable_tokens: int = 0
    total_preserved_tokens: int = 0
    reasons: Dict[str, str] = field(default_factory=dict)   # chunk_id → reason kept/evicted

    def summary(self) -> str:
        return (
            f"Evictable: {len(self.evictable_chunks)} chunks "
            f"({self.total_evictable_tokens} tokens) | "
            f"Preserved: {len(self.preserved_chunks)} chunks "
            f"({self.total_preserved_tokens} tokens)"
        )


class EvictionEngine:
    def __init__(
        self,
        registry: Optional[TaskRegistry] = None,
        tagger: Optional[ContextTagger] = None,
        graph: Optional[ReferenceGraph] = None,
    ):
        self._registry = registry or TaskRegistry()
        self._tagger = tagger or ContextTagger(self._registry)
        self._graph = graph or ReferenceGraph()

    def run(self, update_db: bool = True) -> EvictionReport:
        """
        Evaluate all non-evicted chunks and produce an EvictionReport.
        If update_db=True, marks newly evictable chunks in the DB.
        """
        report = EvictionReport()
        chunks = self._tagger.list_all()
        active_referenced = self._graph.chunks_referenced_by_active_tasks()

        # Build a supersession index: (tool_name, input_hash) → latest chunk_id
        supersession_index = self._build_supersession_index(chunks)

        for chunk in chunks:
            if chunk.status == "evictable":
                # Already marked; include in report as evictable
                report.evictable_chunks.append(chunk)
                report.total_evictable_tokens += chunk.size_tokens
                report.reasons[chunk.id] = "previously_marked_evictable"
                continue

            reason = self._why_keep(chunk, active_referenced, supersession_index)
            if reason is None:
                # Safe to evict
                report.evictable_chunks.append(chunk)
                report.total_evictable_tokens += chunk.size_tokens
                report.reasons[chunk.id] = "all_tasks_complete_no_active_refs"
                if update_db:
                    self._tagger.mark_evictable(chunk.id)
                    log.info("Marked evictable: %s (%d tokens)", chunk.id, chunk.size_tokens)
            else:
                report.preserved_chunks.append(chunk)
                report.total_preserved_tokens += chunk.size_tokens
                report.reasons[chunk.id] = reason

        log.info("Eviction run complete. %s", report.summary())
        return report

    def _why_keep(
        self,
        chunk: ContextChunk,
        active_referenced: Set[str],
        supersession_index: Dict[str, str],
    ) -> Optional[str]:
        """
        Returns a string reason why the chunk must be kept, or None if it can be evicted.
        """
        # Rule 1: check if superseded by a newer identical call
        sig = self._chunk_signature(chunk)
        if sig in supersession_index and supersession_index[sig] != chunk.id:
            # A newer chunk with same tool+input exists → this one is superseded.
            # But only evict if all tasks owning it are complete.
            if self._all_owning_tasks_complete(chunk):
                return None
            return "superseded_but_task_still_active"

        # Rule 2: active reference edge from a live task
        if chunk.id in active_referenced:
            return "referenced_by_active_task"

        # Rule 3: owning tasks not all complete
        if not self._all_owning_tasks_complete(chunk):
            return "owning_task_not_complete"

        # All rules pass → evictable
        return None

    def _all_owning_tasks_complete(self, chunk: ContextChunk) -> bool:
        if not chunk.task_ids:
            # Untagged chunk — treat as orphan; safe to evict after a grace period
            # For now, keep unless we have a clear signal.
            return False
        tasks = [self._registry.get(tid) for tid in chunk.task_ids]
        return all(t is not None and t.is_complete() for t in tasks)

    def _build_supersession_index(
        self, chunks: List[ContextChunk]
    ) -> Dict[str, str]:
        """
        Map (tool_name, serialized_input) → chunk_id of the LATEST chunk with that signature.
        Earlier chunks with the same signature are considered superseded.
        """
        index: Dict[str, str] = {}
        # chunks are ordered by created_at from list_all()
        for chunk in chunks:
            sig = self._chunk_signature(chunk)
            if sig:
                index[sig] = chunk.id  # later entries overwrite earlier
        return index

    def _chunk_signature(self, chunk: ContextChunk) -> Optional[str]:
        """A stable key for deduplication: tool_name + canonicalized input."""
        try:
            canonical_input = json.dumps(chunk.tool_input, sort_keys=True)
            return f"{chunk.tool_name}::{canonical_input}"
        except Exception:
            return None

    def evictable_token_count(self) -> int:
        """Quick query: total tokens in evictable chunks."""
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(size_tokens), 0) FROM context_chunks WHERE status = 'evictable'"
            ).fetchone()
            return row[0]
