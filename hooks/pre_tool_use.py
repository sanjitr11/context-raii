#!/usr/bin/env python3
"""
PreToolUse hook for context-raii.

Receives a JSON event on stdin describing the tool about to be called.
Responsibilities:
  - Watch for TodoWrite / TaskCreate / TaskUpdate calls → update task registry
  - Write a "pending tag" for the upcoming tool_use_id so post_tool_use can
    associate the result with the active task
  - Exit 0 (allow), exit 1 to block (we never block here)

Hook event schema (Claude Code PreToolUse):
{
  "session_id": "...",
  "tool_name": "...",
  "tool_input": { ... },
  "tool_use_id": "..."
}

Output: JSON to stdout (optional additionalContext / decision).
"""

import json
import logging
import sys
from pathlib import Path

# Allow running from the repo root without installing
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raii.task_registry import TaskRegistry
from raii.storage import DB_DIR, ensure_db

DB_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(DB_DIR / "hooks.log"),
    level=logging.INFO,
    format="%(asctime)s [pre_tool_use] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

PENDING_TAG_PATH = DB_DIR / "pending_tag.json"

WORK_TOOLS = frozenset({"Edit", "Write", "Bash", "MultiEdit"})


def main():
    ensure_db()
    try:
        event = json.load(sys.stdin)
    except Exception as e:
        log.error("Failed to parse hook event: %s", e)
        sys.exit(0)

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    tool_use_id = event.get("tool_use_id", "")
    session_id = event.get("session_id", "")

    log.info("pre_tool_use: tool=%s id=%s", tool_name, tool_use_id)

    registry = TaskRegistry()

    # ------------------------------------------------------------------
    # Handle task lifecycle tools
    # ------------------------------------------------------------------
    if tool_name == "TaskCreate":
        _handle_task_create(registry, tool_input)

    elif tool_name == "TaskUpdate":
        _handle_task_update(registry, tool_input)

    elif tool_name == "TodoWrite":
        _handle_todo_write(registry, tool_input)

    # ------------------------------------------------------------------
    # Write a pending tag so post_tool_use knows which task to associate
    # ------------------------------------------------------------------
    active_task = registry.get_current_active()
    pending = {
        "tool_use_id": tool_use_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": session_id,
        "active_task_id": active_task.id if active_task else None,
    }
    DB_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_TAG_PATH.write_text(json.dumps(pending))

    # ------------------------------------------------------------------
    # Enforce task hygiene: always block work tools if no active task.
    # ------------------------------------------------------------------
    output = {}
    if tool_name in WORK_TOOLS and active_task is None:
        output["decision"] = "block"
        output["reason"] = (
            "No active task. You must call TodoWrite to create and start a task "
            "before making changes. Create the task now, then retry."
        )
        log.info("Blocked %s — no active task (session %s)", tool_name, session_id)

    print(json.dumps(output))
    sys.exit(0)


def _handle_task_create(registry: TaskRegistry, tool_input: dict):
    task_id = tool_input.get("id") or tool_input.get("task_id")
    subject = tool_input.get("subject") or tool_input.get("description", "unknown")
    parent_id = tool_input.get("parent_id")
    if task_id:
        registry.create(id=task_id, subject=subject, parent_id=parent_id)
        log.info("TaskCreate: id=%s subject=%r", task_id, subject)


def _handle_task_update(registry: TaskRegistry, tool_input: dict):
    task_id = tool_input.get("taskId") or tool_input.get("id") or tool_input.get("task_id")
    new_status = tool_input.get("status")
    new_subject = tool_input.get("subject")
    if not task_id:
        return

    task = registry.get(task_id)
    if task is None:
        # Auto-create if we missed the creation event
        subject = new_subject or tool_input.get("description", "unknown")
        task = registry.create(id=task_id, subject=subject)

    if new_subject:
        task.subject = new_subject
        registry.upsert(task)

    if new_status:
        registry.update_status(task_id, new_status)
        log.info("TaskUpdate: id=%s status=%s", task_id, new_status)


def _handle_todo_write(registry: TaskRegistry, tool_input: dict):
    todos = tool_input.get("todos", [])
    for todo in todos:
        task_id = todo.get("id")
        if not task_id:
            continue
        existing = registry.get(task_id)
        status = todo.get("status", "pending")
        subject = todo.get("content", todo.get("subject", "unknown"))
        if existing is None:
            registry.create(id=task_id, subject=subject)
        registry.update_status(task_id, status)
    log.info("TodoWrite: processed %d todos", len(todos))


if __name__ == "__main__":
    main()
