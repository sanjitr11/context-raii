"""
Generates structured eviction hints for the PreCompact hook.
Writes ~/.claude/raii/eviction_hints.json for the compaction summary to consume.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .eviction_engine import EvictionEngine, EvictionReport
from .task_registry import TaskRegistry
from .context_tagger import ContextTagger
from .reference_graph import ReferenceGraph
from .storage import DB_DIR, get_conn

log = logging.getLogger(__name__)

HINTS_PATH = DB_DIR / "eviction_hints.json"
COMPLIANCE_MONITOR_PATH = DB_DIR / "compliance_monitor.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CompactionAdvisor:
    """
    Produces eviction_hints.json to guide Claude Code's compaction summary.

    The hints file contains:
    - safe_to_evict: chunks that can be dropped from the summary
    - critical_to_preserve: chunks/tasks that must be retained verbatim
    - active_tasks_summary: human-readable state of in-progress tasks
    - token_savings_estimate: how many tokens the hints could recover
    """

    def __init__(
        self,
        engine: Optional[EvictionEngine] = None,
        registry: Optional[TaskRegistry] = None,
    ):
        self._engine = engine or EvictionEngine()
        self._registry = registry or TaskRegistry()

    def generate_hints(self, update_db: bool = True) -> dict:
        report = self._engine.run(update_db=update_db)
        active_tasks = self._registry.list_active()

        hints = {
            "generated_at": _now(),
            "token_savings_estimate": report.total_evictable_tokens,
            "safe_to_evict": [
                {
                    "chunk_id": c.id,
                    "tool_name": c.tool_name,
                    "size_tokens": c.size_tokens,
                    "is_refetchable": c.is_refetchable,
                    "reason": report.reasons.get(c.id, "evictable"),
                }
                for c in report.evictable_chunks
            ],
            "critical_to_preserve": [
                {
                    "chunk_id": c.id,
                    "tool_name": c.tool_name,
                    "size_tokens": c.size_tokens,
                    "reason": report.reasons.get(c.id, "preserved"),
                }
                for c in report.preserved_chunks
            ],
            "active_tasks_summary": [
                {
                    "id": t.id,
                    "subject": t.subject,
                    "status": t.status,
                    "chunk_count": len(t.context_chunk_ids),
                }
                for t in active_tasks
            ],
            "compaction_guidance": self._build_guidance(report, active_tasks),
        }

        self._write_hints(hints)
        log.info(
            "Wrote eviction hints: %d evictable (%d tokens), %d preserved",
            len(report.evictable_chunks),
            report.total_evictable_tokens,
            len(report.preserved_chunks),
        )
        return hints

    def _build_guidance(self, report: EvictionReport, active_tasks) -> str:
        """
        Human-readable compaction instruction injected into the summary prompt.
        """
        lines = [
            "=== RAII Context Eviction Guidance ===",
            "",
            f"Estimated reclaimable tokens: {report.total_evictable_tokens}",
            "",
        ]

        if active_tasks:
            lines.append("ACTIVE TASKS (must preserve context for these):")
            for t in active_tasks:
                lines.append(f"  • [{t.status.upper()}] {t.subject} (id={t.id})")
            lines.append("")

        if report.evictable_chunks:
            lines.append(
                "SAFE TO OMIT from summary (all owning tasks complete, "
                "no active references):"
            )
            for c in report.evictable_chunks[:20]:  # cap for readability
                refetch = " [re-fetchable]" if c.is_refetchable else ""
                lines.append(
                    f"  • {c.tool_name} result {c.id[:8]}…{refetch} "
                    f"({c.size_tokens} tokens)"
                )
            if len(report.evictable_chunks) > 20:
                lines.append(
                    f"  … and {len(report.evictable_chunks) - 20} more (see eviction_hints.json)"
                )
            lines.append("")

        if report.preserved_chunks:
            lines.append("PRESERVE in summary (still needed):")
            for c in report.preserved_chunks[:10]:
                lines.append(
                    f"  • {c.tool_name} result {c.id[:8]}… "
                    f"({c.size_tokens} tokens) — {report.reasons.get(c.id, '?')}"
                )
            lines.append("")

        lines += [
            "COMPLIANCE REQUEST:",
            "After writing your full summary, append this block verbatim:",
            "=== RAII_COMPLIANCE ===",
            "EVICTED_IDS: <comma-separated chunk IDs you omitted from the summary>",
            "PRESERVED_IDS: <comma-separated chunk IDs you included in the summary>",
            "=== END_RAII_COMPLIANCE ===",
            "",
            "=== End RAII Guidance ===",
        ]
        return "\n".join(lines)

    def _write_hints(self, hints: dict) -> None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        HINTS_PATH.write_text(json.dumps(hints, indent=2))

    def read_hints(self) -> Optional[dict]:
        if not HINTS_PATH.exists():
            return None
        return json.loads(HINTS_PATH.read_text())

    def log_compaction_event(self, hints: dict, session_id: str) -> int:
        """
        Record a compaction event in the DB. Returns the new event ID.
        Called from SessionStart when source == "compact".
        """
        with get_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO compaction_events
                    (session_id, compacted_at, hints_evictable_count,
                     hints_preserved_count, hints_evictable_tokens)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    _now(),
                    len(hints.get("safe_to_evict", [])),
                    len(hints.get("critical_to_preserve", [])),
                    hints.get("token_savings_estimate", 0),
                ),
            )
            return cursor.lastrowid

    def write_compliance_monitor(self, event_id: int, session_id: str, hints: dict) -> None:
        """
        Write a compliance_monitor.json tracking which file paths were in each
        hint category. PostToolUse reads this to detect re-fetches after compaction.
        """
        evictable_paths = _extract_read_paths(hints.get("safe_to_evict", []))
        preserved_paths = _extract_read_paths(hints.get("critical_to_preserve", []))
        monitor = {
            "compaction_event_id": event_id,
            "session_id": session_id,
            "evictable_chunk_ids": [c["chunk_id"] for c in hints.get("safe_to_evict", [])],
            "preserved_chunk_ids": [c["chunk_id"] for c in hints.get("critical_to_preserve", [])],
            "evictable_file_paths": evictable_paths,
            "preserved_file_paths": preserved_paths,
        }
        DB_DIR.mkdir(parents=True, exist_ok=True)
        COMPLIANCE_MONITOR_PATH.write_text(json.dumps(monitor, indent=2))
        log.info(
            "Wrote compliance monitor: %d evictable paths, %d preserved paths",
            len(evictable_paths),
            len(preserved_paths),
        )

    def record_refetch(self, file_path: str) -> None:
        """
        Called from PostToolUse when a Read fires after compaction.
        Checks if the file was in the evictable or preserved hint lists and
        updates the compaction_events record accordingly.

        Re-fetch of evictable path  → confirmed_evicted++  (hint respected, chunk not in summary)
        Re-fetch of preserved path  → false_negatives++    (hint ignored, critical chunk dropped)
        """
        if not COMPLIANCE_MONITOR_PATH.exists():
            return
        try:
            monitor = json.loads(COMPLIANCE_MONITOR_PATH.read_text())
        except Exception:
            return

        event_id = monitor.get("compaction_event_id")
        if not event_id:
            return

        evictable_paths = set(monitor.get("evictable_file_paths", []))
        preserved_paths = set(monitor.get("preserved_file_paths", []))

        in_evictable = file_path in evictable_paths
        in_preserved = file_path in preserved_paths

        if not in_evictable and not in_preserved:
            return

        with get_conn() as conn:
            if in_evictable:
                conn.execute(
                    "UPDATE compaction_events SET confirmed_evicted = confirmed_evicted + 1 WHERE id = ?",
                    (event_id,),
                )
                log.info("Compliance: confirmed eviction for %s (event %d)", file_path, event_id)
            if in_preserved:
                conn.execute(
                    "UPDATE compaction_events SET false_negatives = false_negatives + 1 WHERE id = ?",
                    (event_id,),
                )
                log.info("Compliance: false negative for %s (event %d)", file_path, event_id)

            # Recompute compliance_rate
            row = conn.execute(
                "SELECT confirmed_evicted, hints_evictable_count FROM compaction_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row and row["hints_evictable_count"] > 0:
                rate = row["confirmed_evicted"] / row["hints_evictable_count"]
                conn.execute(
                    "UPDATE compaction_events SET compliance_rate = ? WHERE id = ?",
                    (rate, event_id),
                )

    def read_compliance_monitor(self) -> Optional[dict]:
        if not COMPLIANCE_MONITOR_PATH.exists():
            return None
        try:
            return json.loads(COMPLIANCE_MONITOR_PATH.read_text())
        except Exception:
            return None


def _extract_read_paths(chunk_list: list) -> list:
    """Extract file_path values from the tool_input of Read chunks in a hint list."""
    from .storage import get_conn, deserialize
    paths = []
    chunk_ids = [c["chunk_id"] for c in chunk_list if c.get("chunk_id")]
    if not chunk_ids:
        return paths
    placeholders = ",".join("?" * len(chunk_ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT tool_input FROM context_chunks WHERE id IN ({placeholders}) AND tool_name = 'Read'",
            chunk_ids,
        ).fetchall()
    for row in rows:
        try:
            ti = deserialize(row["tool_input"])
            path = ti.get("file_path")
            if path:
                paths.append(path)
        except Exception:
            pass
    return paths
