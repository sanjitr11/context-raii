"""
Tests for EvictionEngine.

Key invariants:
- A chunk is evictable only when all owning tasks are complete
  and no active task has a reference edge to it.
- Shared chunks (referenced by multiple tasks) only evict when ALL tasks complete.
- Superseded chunks (same tool + input, newer version exists) evict if owning tasks done.
- update_db=True marks chunks as 'evictable' in SQLite.
"""

import pytest
from unittest.mock import patch
from datetime import datetime, timezone


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db_path = tmp_path / "state.db"
    with (
        patch("raii.storage.DB_PATH", db_path),
        patch("raii.storage.DB_DIR", tmp_path),
    ):
        yield db_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts():
    return datetime.now(timezone.utc).isoformat()


def _task(task_id: str, status: str = "in_progress"):
    from raii.task_registry import TaskRegistry
    reg = TaskRegistry()
    reg.create(id=task_id, subject=f"Task {task_id}")
    if status != "pending":
        reg.update_status(task_id, status)
    return reg


def _chunk(
    chunk_id: str,
    tool_name: str = "Read",
    task_id: str = None,
    size: int = 100,
    tool_input: dict = None,
):
    """Insert a chunk and optionally associate it with a task."""
    from raii.storage import get_conn, ensure_db
    ensure_db()
    import json
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO context_chunks "
            "(id, tool_name, tool_input, is_refetchable, status, size_tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                chunk_id,
                tool_name,
                json.dumps(tool_input or {}),
                1 if tool_name in ("Read", "Grep", "Glob") else 0,
                "fresh",
                size,
                _ts(),
            ),
        )
        if task_id:
            conn.execute(
                "INSERT OR IGNORE INTO task_chunks (task_id, chunk_id, tagged_at) VALUES (?, ?, ?)",
                (task_id, chunk_id, _ts()),
            )


def _engine(registry=None, tagger=None, graph=None):
    from raii.eviction_engine import EvictionEngine
    from raii.task_registry import TaskRegistry
    from raii.context_tagger import ContextTagger
    from raii.reference_graph import ReferenceGraph
    reg = registry or TaskRegistry()
    tag = tagger or ContextTagger(reg)
    gr = graph or ReferenceGraph()
    return EvictionEngine(registry=reg, tagger=tag, graph=gr)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBasicEviction:
    def test_chunk_evictable_when_task_complete(self):
        reg = _task("t1", "completed")
        _chunk("c1", task_id="t1")
        report = _engine(registry=reg).run(update_db=False)
        evictable_ids = {c.id for c in report.evictable_chunks}
        assert "c1" in evictable_ids

    def test_chunk_not_evictable_when_task_in_progress(self):
        reg = _task("t1", "in_progress")
        _chunk("c1", task_id="t1")
        report = _engine(registry=reg).run(update_db=False)
        evictable_ids = {c.id for c in report.evictable_chunks}
        assert "c1" not in evictable_ids

    def test_untagged_chunk_not_evictable(self):
        """Chunks with no task association should be kept (unknown ownership)."""
        _chunk("c_orphan")
        report = _engine().run(update_db=False)
        evictable_ids = {c.id for c in report.evictable_chunks}
        assert "c_orphan" not in evictable_ids


class TestSharedChunkRefCounting:
    def test_shared_chunk_kept_if_one_task_active(self):
        """c1 owned by t1 (done) AND t2 (active) → not evictable."""
        from raii.task_registry import TaskRegistry
        reg = TaskRegistry()
        reg.create("t1", "Task 1")
        reg.create("t2", "Task 2")
        reg.update_status("t1", "completed")
        # t2 stays in_progress
        _chunk("c_shared", task_id="t1")
        # Also tag to t2
        from raii.storage import get_conn
        with get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO task_chunks (task_id, chunk_id, tagged_at) VALUES (?, ?, ?)",
                ("t2", "c_shared", _ts()),
            )
        report = _engine(registry=reg).run(update_db=False)
        evictable_ids = {c.id for c in report.evictable_chunks}
        assert "c_shared" not in evictable_ids

    def test_shared_chunk_evictable_when_all_tasks_complete(self):
        from raii.task_registry import TaskRegistry
        reg = TaskRegistry()
        reg.create("t1", "Task 1")
        reg.create("t2", "Task 2")
        reg.update_status("t1", "completed")
        reg.update_status("t2", "completed")
        _chunk("c_shared", task_id="t1")
        from raii.storage import get_conn
        with get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO task_chunks (task_id, chunk_id, tagged_at) VALUES (?, ?, ?)",
                ("t2", "c_shared", _ts()),
            )
        report = _engine(registry=reg).run(update_db=False)
        evictable_ids = {c.id for c in report.evictable_chunks}
        assert "c_shared" in evictable_ids


class TestReferenceEdgeBlocking:
    def test_ref_edge_from_active_task_blocks_eviction(self):
        """Even if c1 belongs to a completed task, an active reference edge blocks eviction."""
        from raii.task_registry import TaskRegistry
        from raii.reference_graph import ReferenceGraph
        reg = TaskRegistry()
        reg.create("t1", "Done task")
        reg.create("t2", "Active task")
        reg.update_status("t1", "completed")
        # t2 stays in_progress

        _chunk("c1", task_id="t1")
        graph = ReferenceGraph()
        graph.add_edge("t2", "c1", "cited_in_reasoning")

        report = _engine(registry=reg, graph=graph).run(update_db=False)
        evictable_ids = {c.id for c in report.evictable_chunks}
        assert "c1" not in evictable_ids

    def test_ref_edge_from_completed_task_does_not_block(self):
        from raii.task_registry import TaskRegistry
        from raii.reference_graph import ReferenceGraph
        reg = TaskRegistry()
        reg.create("t1", "Task 1")
        reg.create("t2", "Task 2")
        reg.update_status("t1", "completed")
        reg.update_status("t2", "completed")

        _chunk("c1", task_id="t1")
        graph = ReferenceGraph()
        graph.add_edge("t2", "c1", "builds_on")

        report = _engine(registry=reg, graph=graph).run(update_db=False)
        evictable_ids = {c.id for c in report.evictable_chunks}
        assert "c1" in evictable_ids


class TestSupersession:
    def test_superseded_chunk_evictable_when_task_done(self):
        """Two chunks with same tool+input; earlier one is superseded."""
        import json
        from raii.task_registry import TaskRegistry
        reg = TaskRegistry()
        reg.create("t1", "Task")
        reg.update_status("t1", "completed")

        same_input = {"file_path": "/foo/bar.py"}
        # c_old created first
        _chunk("c_old", tool_name="Read", task_id="t1", tool_input=same_input)
        # c_new created later with identical tool+input
        _chunk("c_new", tool_name="Read", task_id="t1", tool_input=same_input)

        report = _engine(registry=reg).run(update_db=False)
        evictable_ids = {c.id for c in report.evictable_chunks}
        # Both should be evictable since task is complete
        assert "c_old" in evictable_ids
        assert "c_new" in evictable_ids

    def test_superseded_chunk_kept_if_task_active(self):
        import json
        from raii.task_registry import TaskRegistry
        reg = TaskRegistry()
        reg.create("t1", "Task")
        reg.update_status("t1", "in_progress")

        same_input = {"file_path": "/x.py"}
        _chunk("c_old", tool_name="Read", task_id="t1", tool_input=same_input)
        _chunk("c_new", tool_name="Read", task_id="t1", tool_input=same_input)

        report = _engine(registry=reg).run(update_db=False)
        evictable_ids = {c.id for c in report.evictable_chunks}
        assert "c_old" not in evictable_ids
        assert "c_new" not in evictable_ids


class TestUpdateDb:
    def test_update_db_marks_evictable_in_sqlite(self):
        reg = _task("t1", "completed")
        _chunk("c1", task_id="t1")
        _engine(registry=reg).run(update_db=True)

        from raii.context_tagger import ContextTagger
        from raii.task_registry import TaskRegistry
        tagger = ContextTagger(TaskRegistry())
        chunk = tagger.get("c1")
        assert chunk.status == "evictable"

    def test_no_update_db_leaves_status_unchanged(self):
        reg = _task("t1", "completed")
        _chunk("c1", task_id="t1")
        _engine(registry=reg).run(update_db=False)

        from raii.context_tagger import ContextTagger
        from raii.task_registry import TaskRegistry
        tagger = ContextTagger(TaskRegistry())
        chunk = tagger.get("c1")
        assert chunk.status == "fresh"


class TestWriteInvalidation:
    def test_edit_immediately_evicts_prior_read(self):
        """A Read chunk for a file becomes evictable the moment that file is edited."""
        from raii.task_registry import TaskRegistry
        from raii.context_tagger import ContextTagger

        reg = TaskRegistry()
        reg.create("t1", "Task 1")
        reg.update_status("t1", "in_progress")

        tagger = ContextTagger(reg)
        # Simulate a Read chunk for auth.py — task still active so normally NOT evictable
        _chunk("c_read", tool_name="Read", task_id="t1",
               tool_input={"file_path": "/src/auth.py"})

        # Now the file is edited — prior read is stale
        n = tagger.invalidate_reads_for_path("/src/auth.py")
        assert n == 1

        chunk = tagger.get("c_read")
        assert chunk.status == "evictable"

    def test_edit_does_not_evict_read_of_different_file(self):
        from raii.task_registry import TaskRegistry
        from raii.context_tagger import ContextTagger

        reg = TaskRegistry()
        reg.create("t1", "Task 1")
        reg.update_status("t1", "in_progress")
        tagger = ContextTagger(reg)

        _chunk("c_read", tool_name="Read", task_id="t1",
               tool_input={"file_path": "/src/models.py"})

        n = tagger.invalidate_reads_for_path("/src/auth.py")
        assert n == 0

        chunk = tagger.get("c_read")
        assert chunk.status == "fresh"

    def test_already_evictable_reads_not_double_counted(self):
        from raii.task_registry import TaskRegistry
        from raii.context_tagger import ContextTagger

        reg = TaskRegistry()
        reg.create("t1", "Task 1")
        reg.update_status("t1", "completed")
        tagger = ContextTagger(reg)

        _chunk("c_read", tool_name="Read", task_id="t1",
               tool_input={"file_path": "/src/auth.py"})
        tagger.mark_evictable("c_read")  # already evicted by task completion

        # Edit fires — should not fail, just returns 0 (already evictable)
        n = tagger.invalidate_reads_for_path("/src/auth.py")
        assert n == 0


class TestDeclaredDependencies:
    def test_dependency_pins_chunks_until_dependent_completes(self):
        """Task A completes, but Task B depends on it → A's chunks stay pinned."""
        from raii.task_registry import TaskRegistry

        reg = TaskRegistry()
        reg.create("task-a", "Design auth module")
        reg.update_status("task-a", "completed")
        reg.create("task-b", "Implement auth module")
        reg.update_status("task-b", "in_progress")
        reg.add_dependency("task-b", "task-a")  # B depends on A

        _chunk("c1", task_id="task-a")

        report = _engine(registry=reg).run(update_db=False)
        evictable_ids = {c.id for c in report.evictable_chunks}
        assert "c1" not in evictable_ids
        assert report.reasons.get("c1") == "active_dependent_task"

    def test_dependency_released_when_dependent_completes(self):
        """Once Task B also completes, Task A's chunks become evictable."""
        from raii.task_registry import TaskRegistry

        reg = TaskRegistry()
        reg.create("task-a", "Design auth module")
        reg.update_status("task-a", "completed")
        reg.create("task-b", "Implement auth module")
        reg.update_status("task-b", "completed")
        reg.add_dependency("task-b", "task-a")

        _chunk("c1", task_id="task-a")

        report = _engine(registry=reg).run(update_db=False)
        evictable_ids = {c.id for c in report.evictable_chunks}
        assert "c1" in evictable_ids

    def test_no_dependency_evicts_normally(self):
        """Without a dependency declaration, completed task chunks evict as usual."""
        from raii.task_registry import TaskRegistry

        reg = TaskRegistry()
        reg.create("task-a", "Design")
        reg.update_status("task-a", "completed")

        _chunk("c1", task_id="task-a")

        report = _engine(registry=reg).run(update_db=False)
        evictable_ids = {c.id for c in report.evictable_chunks}
        assert "c1" in evictable_ids


class TestTokenCounting:
    def test_report_token_counts(self):
        reg = _task("t1", "completed")
        _chunk("c1", task_id="t1", size=500)
        _chunk("c2", task_id="t1", size=300)
        report = _engine(registry=reg).run(update_db=False)
        assert report.total_evictable_tokens == 800

    def test_evictable_token_count_method(self):
        reg = _task("t1", "completed")
        _chunk("c1", task_id="t1", size=400)
        engine = _engine(registry=reg)
        engine.run(update_db=True)
        assert engine.evictable_token_count() == 400
