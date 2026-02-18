"""Tests for TaskRegistry."""

import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import patch

# Redirect DB to a temp file for test isolation
_tmp = tempfile.mktemp(suffix=".db")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Use a fresh SQLite DB for each test."""
    db_path = tmp_path / "state.db"
    db_dir = tmp_path
    with (
        patch("raii.storage.DB_PATH", db_path),
        patch("raii.storage.DB_DIR", db_dir),
    ):
        yield db_path


def _make_registry():
    from raii.task_registry import TaskRegistry
    return TaskRegistry()


class TestTaskCreate:
    def test_create_returns_task(self):
        reg = _make_registry()
        task = reg.create(id="t1", subject="Do something")
        assert task.id == "t1"
        assert task.subject == "Do something"
        assert task.status == "pending"
        assert task.completed_at is None

    def test_create_persists(self):
        reg = _make_registry()
        reg.create(id="t1", subject="Persisted task")
        reg2 = _make_registry()
        t = reg2.get("t1")
        assert t is not None
        assert t.subject == "Persisted task"

    def test_create_with_parent(self):
        reg = _make_registry()
        reg.create(id="parent", subject="Parent")
        child = reg.create(id="child", subject="Child", parent_id="parent")
        assert child.parent_id == "parent"

    def test_get_nonexistent_returns_none(self):
        reg = _make_registry()
        assert reg.get("nope") is None


class TestTaskStatus:
    def test_update_to_in_progress(self):
        reg = _make_registry()
        reg.create(id="t1", subject="Task")
        updated = reg.update_status("t1", "in_progress")
        assert updated.status == "in_progress"
        assert updated.completed_at is None

    def test_update_to_completed_sets_timestamp(self):
        reg = _make_registry()
        reg.create(id="t1", subject="Task")
        updated = reg.update_status("t1", "completed")
        assert updated.status == "completed"
        assert updated.completed_at is not None

    def test_update_nonexistent_returns_none(self):
        reg = _make_registry()
        assert reg.update_status("ghost", "completed") is None

    def test_is_active_and_complete(self):
        reg = _make_registry()
        reg.create(id="t1", subject="Task")
        t = reg.get("t1")
        assert t.is_active()
        assert not t.is_complete()

        reg.update_status("t1", "completed")
        t = reg.get("t1")
        assert not t.is_active()
        assert t.is_complete()


class TestListActive:
    def test_list_active_filters_completed(self):
        reg = _make_registry()
        reg.create(id="t1", subject="Active")
        reg.create(id="t2", subject="Done")
        reg.update_status("t2", "completed")

        active = reg.list_active()
        ids = {t.id for t in active}
        assert "t1" in ids
        assert "t2" not in ids

    def test_list_all_includes_completed(self):
        reg = _make_registry()
        reg.create(id="t1", subject="Task1")
        reg.create(id="t2", subject="Task2")
        reg.update_status("t2", "completed")
        all_tasks = reg.list_all()
        assert len(all_tasks) == 2


def _seed_chunk_row(chunk_id: str):
    """Insert a minimal chunk row to satisfy the FK constraint."""
    from raii.storage import get_conn, ensure_db
    from datetime import datetime, timezone
    ensure_db()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO context_chunks "
            "(id, tool_name, is_refetchable, status, size_tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chunk_id, "Bash", 0, "fresh", 100, datetime.now(timezone.utc).isoformat()),
        )


class TestChunkTagging:
    def test_tag_chunk_associates_with_task(self):
        reg = _make_registry()
        reg.create(id="t1", subject="Task")
        _seed_chunk_row("chunk-abc")
        reg.tag_chunk("t1", "chunk-abc")

        chunks = reg.chunks_for_task("t1")
        assert "chunk-abc" in chunks

    def test_tasks_for_chunk(self):
        reg = _make_registry()
        reg.create(id="t1", subject="Task")
        reg.create(id="t2", subject="Task2")
        _seed_chunk_row("shared-chunk")
        reg.tag_chunk("t1", "shared-chunk")
        reg.tag_chunk("t2", "shared-chunk")

        from raii.task_registry import TaskRegistry
        reg2 = TaskRegistry()
        tasks = reg2.tasks_for_chunk("shared-chunk")
        assert {t.id for t in tasks} == {"t1", "t2"}


class TestGetCurrentActive:
    def test_returns_in_progress_task(self):
        reg = _make_registry()
        reg.create(id="t1", subject="T1")
        reg.update_status("t1", "in_progress")
        active = reg.get_current_active()
        assert active is not None
        assert active.id == "t1"

    def test_returns_none_when_all_complete(self):
        reg = _make_registry()
        reg.create(id="t1", subject="T1")
        reg.update_status("t1", "completed")
        active = reg.get_current_active()
        assert active is None
