#!/usr/bin/env python3
"""
SessionStart hook for context-raii.

Fires at the beginning of each Claude Code session (or after compaction restore).
Responsibilities:
  - If source == "compact": inject a task registry summary as additionalContext
    so the new session window knows what tasks are active and what was completed
  - Always: ensure DB is initialized

Hook event schema (Claude Code SessionStart):
{
  "session_id": "...",
  "source": "startup" | "compact"
}

Output JSON to stdout:
{
  "additionalContext": "...state summary..."
}
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raii.task_registry import TaskRegistry
from raii.context_tagger import ContextTagger
from raii.compaction_advisor import CompactionAdvisor
from raii.storage import DB_DIR, ensure_db

DB_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(DB_DIR / "hooks.log"),
    level=logging.INFO,
    format="%(asctime)s [session_start] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def main():
    ensure_db()
    try:
        event = json.load(sys.stdin)
    except Exception as e:
        log.error("Failed to parse hook event: %s", e)
        print(json.dumps({"additionalContext": ""}))
        sys.exit(0)

    source = event.get("source", "startup")
    session_id = event.get("session_id", "")
    log.info("session_start: source=%s session_id=%s", source, session_id)

    parts = []

    parts.append(
        "REQUIRED WORKFLOW: You must use TodoWrite to manage tasks in this session.\n"
        "1. Before doing any work, call TodoWrite to create tasks for everything you plan to do.\n"
        "2. Mark a task in_progress with TodoWrite before starting it.\n"
        "3. Mark it completed with TodoWrite as soon as you finish it.\n"
        "Do not make any file edits, run any commands, or do any implementation work "
        "before first creating a task for it with TodoWrite. This is mandatory."
    )

    if source == "compact":
        parts.append(_build_post_compaction_summary())
        log.info("Injecting post-compaction summary")

    print(json.dumps({"additionalContext": "\n\n".join(parts)}))
    sys.exit(0)


def _build_post_compaction_summary() -> str:
    registry = TaskRegistry()
    tagger = ContextTagger(registry)
    advisor = CompactionAdvisor()

    all_tasks = registry.list_all()
    active_tasks = registry.list_active()
    completed_tasks = [t for t in all_tasks if t.is_complete()]

    all_chunks = tagger.list_all()
    evictable = [c for c in all_chunks if c.status == "evictable"]
    fresh = [c for c in all_chunks if c.status == "fresh"]

    # Try to read last hints for context
    hints = advisor.read_hints()
    savings = hints.get("token_savings_estimate", 0) if hints else 0

    lines = [
        "=== RAII Context-RAII State Summary (post-compaction) ===",
        "",
        f"Tasks: {len(active_tasks)} active, {len(completed_tasks)} completed",
        "",
    ]

    if active_tasks:
        lines.append("ACTIVE TASKS (context required):")
        for t in active_tasks:
            lines.append(f"  [{t.status.upper()}] {t.subject} (id={t.id})")
        lines.append("")

    if completed_tasks:
        lines.append("RECENTLY COMPLETED TASKS (context may be minimal):")
        # Show last 5 completed
        for t in sorted(completed_tasks, key=lambda x: x.completed_at or "", reverse=True)[:5]:
            lines.append(f"  [DONE] {t.subject} (id={t.id})")
        lines.append("")

    lines += [
        f"Context chunks: {len(fresh)} fresh, {len(evictable)} evictable",
        f"Estimated evictable tokens: {savings}",
        "",
        "The context-raii system is active. Task completion will continue to",
        "update the eviction state and guide future compactions.",
        "",
        "=== End RAII State Summary ===",
    ]

    return "\n".join(lines)


if __name__ == "__main__":
    main()
