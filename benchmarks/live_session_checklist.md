# Live Session Test Checklist

Run this after enabling the real hooks on a test project.
Check each item in order — they're sequenced so each confirms the previous works.

## Setup
```bash
# Enable schema probe FIRST (not the real hooks)
cp ~/context-raii/.claude/settings.schema_probe.json ~/test-project/.claude/settings.json

# Start a Claude Code session in the test project
# Do 3-4 tasks that each use Read/Grep/Bash
# Then stop and inspect:
cat ~/.claude/raii/schema_samples.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    r = json.loads(line)
    print(r['hook'], r['keys'], 'result_type:', r['tool_result_type'])
"
```

## Checkpoint 1: Hooks fire at all
- [ ] `schema_samples.jsonl` exists and has entries
- [ ] All four hook types appear: pre_tool_use, post_tool_use, pre_compact (if you ran /compact), session_start
- [ ] `tool_use_id` is present in pre_tool_use events
- [ ] `tool_result` is present in post_tool_use events — check the type (string vs list)

**If tool_result is a list:** update `_extract_text()` in `post_tool_use.py` — the current
handler covers it but verify the actual structure matches.

**If TaskUpdate events use different field names for taskId:** check the field name in
`pre_tool_use.py`'s `_handle_task_update()` — the hook tries `taskId`, `id`, and `task_id`.

## Checkpoint 2: Switch to real hooks
```bash
cp ~/context-raii/.claude/settings.json ~/test-project/.claude/settings.json
```

Run a fresh session. After any tool call:
```bash
sqlite3 ~/.claude/raii/state.db "SELECT id, tool_name, status, size_tokens FROM context_chunks ORDER BY created_at DESC LIMIT 5;"
```
- [ ] Rows appear after tool calls (chunks are being ingested)
- [ ] `task_chunks` table has entries linking chunks to tasks

```bash
sqlite3 ~/.claude/raii/state.db "SELECT * FROM tasks;"
```
- [ ] Tasks appear when Claude Code's TodoWrite / TaskCreate / TaskUpdate fires
- [ ] If no tasks appear: the task tool names may differ — check schema_samples.jsonl for the actual tool name used

## Checkpoint 3: Eviction triggers on task completion
Mark a task complete in Claude Code (or say "mark task X as done").
```bash
sqlite3 ~/.claude/raii/state.db \
  "SELECT status, COUNT(*), SUM(size_tokens) FROM context_chunks GROUP BY status;"
```
- [ ] Some chunks move to `evictable` after the task completes
- [ ] Chunks tagged to still-active tasks stay `fresh`

Also check the log:
```bash
grep "Eviction after task" ~/.claude/raii/hooks.log | tail -5
```

## Checkpoint 4: PreCompact fires correctly
Run `/compact` in Claude Code.
```bash
ls -la ~/.claude/raii/eviction_hints.json
cat ~/.claude/raii/eviction_hints.json | python3 -m json.tool | head -30
```
- [ ] `eviction_hints.json` was updated (check timestamp)
- [ ] `safe_to_evict` list is non-empty if you completed any tasks
- [ ] `active_tasks_summary` lists the right tasks

## Checkpoint 5: Session restore (post-compaction)
After `/compact`, the next session should receive the state summary.
```bash
grep "session_start" ~/.claude/raii/hooks.log | tail -3
grep "Injecting post-compaction" ~/.claude/raii/hooks.log | tail -3
```
- [ ] `session_start` fires on session open
- [ ] If source=compact, the injection log line appears

## What to measure across a real session
```bash
# Run this periodically during a long session
watch -n 30 python3 ~/context-raii/benchmarks/measure_session.py
```

Key numbers to track:
- **Evictable %** after each task completion — should climb steadily
- **Chunks preserved for active tasks** — should be much smaller than total
- Whether you reach the compaction threshold before/after enabling hooks
  (run two comparable sessions and compare when /compact fires)
