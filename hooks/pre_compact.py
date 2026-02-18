#!/usr/bin/env python3
"""
PreCompact hook for context-raii.

Fires just before Claude Code runs a compaction summary.
Responsibilities:
  - Run the eviction engine one final time
  - Write eviction_hints.json to disk
  - Inject a structured compaction guidance string as additionalContext

Hook event schema (Claude Code PreCompact):
{
  "session_id": "...",
  "trigger": "manual" | "auto",
  "context_window_tokens": 123456
}

Output JSON to stdout:
{
  "additionalContext": "...guidance string..."
}
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raii.compaction_advisor import CompactionAdvisor
from raii.storage import DB_DIR, ensure_db

DB_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(DB_DIR / "hooks.log"),
    level=logging.INFO,
    format="%(asctime)s [pre_compact] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def main():
    ensure_db()
    try:
        event = json.load(sys.stdin)
    except Exception as e:
        log.error("Failed to parse hook event: %s", e)
        # Output empty context and continue
        print(json.dumps({"additionalContext": ""}))
        sys.exit(0)

    trigger = event.get("trigger", "unknown")
    context_tokens = event.get("context_window_tokens", 0)
    log.info("pre_compact fired: trigger=%s context_tokens=%d", trigger, context_tokens)

    advisor = CompactionAdvisor()
    try:
        hints = advisor.generate_hints(update_db=True)
        guidance = hints.get("compaction_guidance", "")
        savings = hints.get("token_savings_estimate", 0)
        log.info("Generated hints: %d evictable tokens", savings)
    except Exception as e:
        log.exception("Error generating hints: %s", e)
        guidance = ""

    output = {"additionalContext": guidance}
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
