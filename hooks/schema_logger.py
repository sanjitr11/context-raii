#!/usr/bin/env python3
"""
Drop-in hook for learning the actual event schemas Claude Code sends.
Wire this up INSTEAD OF the real hooks first:

  "PreToolUse":  [{"hooks": [{"type": "command", "command": "python3 ~/context-raii/hooks/schema_logger.py pre_tool_use"}]}]
  "PostToolUse": [{"hooks": [{"type": "command", "command": "python3 ~/context-raii/hooks/schema_logger.py post_tool_use"}]}]
  "PreCompact":  [{"hooks": [{"type": "command", "command": "python3 ~/context-raii/hooks/schema_logger.py pre_compact"}]}]
  "SessionStart":[{"hooks": [{"type": "command", "command": "python3 ~/context-raii/hooks/schema_logger.py session_start"}]}]

Run a session, then inspect:
  cat ~/.claude/raii/schema_samples.jsonl | python3 -m json.tool | less
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG = Path.home() / ".claude" / "raii" / "schema_samples.jsonl"
LOG.parent.mkdir(parents=True, exist_ok=True)

hook_name = sys.argv[1] if len(sys.argv) > 1 else "unknown"

try:
    event = json.load(sys.stdin)
except Exception as e:
    event = {"_parse_error": str(e)}

record = {
    "hook": hook_name,
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "event": event,
    # Top-level keys present
    "keys": list(event.keys()) if isinstance(event, dict) else [],
    # tool_response type and structure (actual field name in Claude Code)
    "tool_result_type": type(event.get("tool_response")).__name__ if "tool_response" in event else None,
    "tool_result_preview": str(event.get("tool_response", ""))[:200] if "tool_response" in event else None,
}

with open(LOG, "a") as f:
    f.write(json.dumps(record) + "\n")

# Pass through — don't block the tool call
print(json.dumps({"additionalContext": ""}))
sys.exit(0)
