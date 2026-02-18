# context-raii

Task-scoped context management for Claude Code. Context chunks get tagged to the active task at ingestion; when a task completes a hook fires that marks those chunks as evictable. The PreCompact hook injects structured eviction hints to guide the compaction summary toward preserving what matters and dropping what doesn't.

## Architecture

```
raii/
  storage.py           # SQLite at ~/.claude/raii/state.db
  task_registry.py     # Task CRUD and status tracking
  context_tagger.py    # Tags tool results with active task ID
  reference_graph.py   # Dependency edges (task → chunk)
  eviction_engine.py   # Evictability logic
  compaction_advisor.py# Generates hints for PreCompact hook
hooks/
  pre_tool_use.py      # Watches TaskCreate/Update, writes pending tag
  post_tool_use.py     # Tags results, runs eviction on completion
  pre_compact.py       # Writes eviction_hints.json, injects guidance
  session_start.py     # Re-injects state summary after compaction
tests/
  test_task_registry.py
  test_reference_graph.py
  test_eviction_engine.py
```

## Eviction Rules

A chunk is evictable when:
1. All tasks in `chunk.task_ids` have `status = completed`
2. No pending/in-progress task has a `ReferenceEdge` pointing to this chunk
3. OR it has been superseded (newer call to same tool + args exists) **and** condition 1 holds

## Setup

### Install (editable)
```bash
cd ~/context-raii
pip install -e ".[dev]"
```

### Run tests
```bash
pytest
```

### Enable hooks

Copy or symlink `.claude/settings.json` into the project where you want tracking:
```bash
cp ~/context-raii/.claude/settings.json ~/your-project/.claude/settings.json
```

Or register globally at `~/.claude/settings.json` (merges with existing config).

### Inspect state
```bash
sqlite3 ~/.claude/raii/state.db "SELECT id, subject, status FROM tasks;"
sqlite3 ~/.claude/raii/state.db "SELECT id, tool_name, status, size_tokens FROM context_chunks ORDER BY created_at DESC LIMIT 20;"
sqlite3 ~/.claude/raii/state.db "SELECT * FROM reference_edges;"
```

### Check eviction hints
```bash
cat ~/.claude/raii/eviction_hints.json | python3 -m json.tool
```

### Watch the log
```bash
tail -f ~/.claude/raii/hooks.log
```

## Measurement

After running a session with hooks active:
```bash
sqlite3 ~/.claude/raii/state.db \
  "SELECT status, SUM(size_tokens), COUNT(*) FROM context_chunks GROUP BY status;"
```

This tells you how many tokens have been freed (evictable) vs. still needed (fresh/integrated).
