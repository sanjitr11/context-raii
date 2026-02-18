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
from .storage import DB_DIR

log = logging.getLogger(__name__)

HINTS_PATH = DB_DIR / "eviction_hints.json"


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

        lines.append("=== End RAII Guidance ===")
        return "\n".join(lines)

    def _write_hints(self, hints: dict) -> None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        HINTS_PATH.write_text(json.dumps(hints, indent=2))

    def read_hints(self) -> Optional[dict]:
        if not HINTS_PATH.exists():
            return None
        return json.loads(HINTS_PATH.read_text())
