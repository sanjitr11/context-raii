#!/usr/bin/env python3
"""
PostToolUse hook for context-raii.

Receives a JSON event on stdin with the tool result.
Responsibilities:
  - Tag the tool result (ContextChunk) to the active task
  - If this was a TaskUpdate(status=completed), run eviction engine
  - Log evictable tokens freed

Hook event schema (Claude Code PostToolUse):
{
  "session_id": "...",
  "tool_name": "...",
  "tool_input": { ... },
  "tool_use_id": "...",
  "tool_response": { ... }   (dict — structure varies by tool)
}
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raii.task_registry import TaskRegistry
from raii.context_tagger import ContextTagger
from raii.eviction_engine import EvictionEngine
from raii.storage import DB_DIR, ensure_db

DB_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(DB_DIR / "hooks.log"),
    level=logging.INFO,
    format="%(asctime)s [post_tool_use] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

PENDING_TAG_PATH = DB_DIR / "pending_tag.json"


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
    tool_response = event.get("tool_response", "")

    result_text = _extract_text(tool_response)

    log.info("post_tool_use: tool=%s id=%s result_len=%d", tool_name, tool_use_id, len(result_text))

    # Read pending tag written by pre_tool_use
    active_task_id = _read_pending_task_id(tool_use_id)

    registry = TaskRegistry()
    tagger = ContextTagger(registry)

    # TaskCreate and TaskUpdate(in_progress) fire before the task is active,
    # so pre_tool_use captures active_task_id=None. Override here using the
    # task ID from the input so these metadata chunks aren't left as orphans.
    if active_task_id is None:
        if tool_name == "TaskCreate":
            active_task_id = tool_input.get("id") or tool_input.get("task_id")
        elif tool_name == "TaskUpdate":
            new_status = tool_input.get("status")
            if new_status == "in_progress":
                active_task_id = (
                    tool_input.get("taskId")
                    or tool_input.get("id")
                    or tool_input.get("task_id")
                )

    # ------------------------------------------------------------------
    # Tag the result as a ContextChunk
    # ------------------------------------------------------------------
    if tool_use_id and result_text:
        chunk = tagger.ingest(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=result_text,
            session_id=session_id,
            task_id=active_task_id,
        )
        log.info(
            "Tagged chunk %s → task=%s (%d tokens, refetchable=%s)",
            chunk.id,
            active_task_id,
            chunk.size_tokens,
            chunk.is_refetchable,
        )

    # ------------------------------------------------------------------
    # Write-invalidation: stale reads of any edited file
    # ------------------------------------------------------------------
    if tool_name in ("Edit", "Write", "MultiEdit"):
        for path in _extract_edited_paths(tool_input):
            n = tagger.invalidate_reads_for_path(path)
            if n > 0:
                log.info("Write-invalidated %d Read chunk(s) for %s", n, path)

    # ------------------------------------------------------------------
    # On task completion: run eviction engine
    # ------------------------------------------------------------------
    if tool_name == "TaskUpdate":
        new_status = tool_input.get("status")
        if new_status == "completed":
            task_id = (
                tool_input.get("taskId")
                or tool_input.get("id")
                or tool_input.get("task_id")
            )
            log.info("Task %s completed — running eviction engine", task_id)
            engine = EvictionEngine(registry=registry, tagger=tagger)
            report = engine.run(update_db=True)
            log.info(
                "Eviction after task %s: %s",
                task_id,
                report.summary(),
            )

    sys.exit(0)


def _extract_text(response) -> str:
    """
    Extract a plain-text representation from a tool_response dict.
    The structure varies by tool:
      Read:       {'type': 'text', 'file': {'filePath': ..., 'content': ...}}
      Bash:       {'stdout': ..., 'stderr': ..., 'interrupted': bool, ...}
      Edit/Write: {'filePath': ..., 'oldString': ..., 'newString': ...}
      Grep/Glob:  {'type': 'text', ...} or similar
      other:      fall back to JSON dump
    """
    if isinstance(response, str):
        return response
    if isinstance(response, list):
        return "\n".join(_extract_text(item) for item in response)
    if not isinstance(response, dict):
        return str(response) if response is not None else ""

    # Read tool
    if "file" in response and isinstance(response["file"], dict):
        return response["file"].get("content", "")

    # Bash tool
    if "stdout" in response or "stderr" in response:
        parts = []
        if response.get("stdout"):
            parts.append(response["stdout"])
        if response.get("stderr"):
            parts.append(response["stderr"])
        return "\n".join(parts)

    # Edit/Write — record the file path + size as the meaningful signal
    if "filePath" in response:
        new = response.get("newString", response.get("content", ""))
        return new if new else f"edited:{response['filePath']}"

    # Generic text block
    if "text" in response:
        return response["text"]

    return json.dumps(response)


def _extract_edited_paths(tool_input: dict) -> list:
    """Extract all file paths touched by an Edit, Write, or MultiEdit call."""
    paths = []
    if "file_path" in tool_input:
        paths.append(tool_input["file_path"])
    # MultiEdit passes a list of edits, each with their own file_path
    for edit in tool_input.get("edits", []):
        if "file_path" in edit:
            paths.append(edit["file_path"])
    return paths


def _read_pending_task_id(tool_use_id: str) -> str | None:
    """
    Read the pending tag written by pre_tool_use for this tool_use_id.
    Falls back to None if the file is missing or mismatched.
    """
    try:
        if PENDING_TAG_PATH.exists():
            data = json.loads(PENDING_TAG_PATH.read_text())
            if data.get("tool_use_id") == tool_use_id:
                return data.get("active_task_id")
    except Exception as e:
        log.warning("Could not read pending tag: %s", e)
    return None


if __name__ == "__main__":
    main()
