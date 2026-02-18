"""Tests for ReferenceGraph."""

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


def _seed_task(task_id: str, status: str = "in_progress"):
    """Insert a task directly into the DB."""
    from raii.storage import get_conn, ensure_db
    ensure_db()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tasks (id, subject, status, created_at) VALUES (?, ?, ?, ?)",
            (task_id, f"Task {task_id}", status, datetime.now(timezone.utc).isoformat()),
        )


def _seed_chunk(chunk_id: str):
    """Insert a chunk directly into the DB."""
    from raii.storage import get_conn, ensure_db
    ensure_db()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO context_chunks "
            "(id, tool_name, is_refetchable, status, size_tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chunk_id, "Read", 1, "fresh", 200, datetime.now(timezone.utc).isoformat()),
        )


def _make_graph():
    from raii.reference_graph import ReferenceGraph
    return ReferenceGraph()


class TestAddEdge:
    def test_add_and_retrieve_edge(self):
        _seed_task("t1")
        _seed_chunk("c1")
        graph = _make_graph()
        graph.add_edge("t1", "c1", "cited_in_reasoning")
        chunks = graph.chunks_referenced_by_task("t1")
        assert "c1" in chunks

    def test_add_duplicate_edge_is_idempotent(self):
        _seed_task("t1")
        _seed_chunk("c1")
        graph = _make_graph()
        graph.add_edge("t1", "c1", "cited_in_reasoning")
        graph.add_edge("t1", "c1", "cited_in_reasoning")  # no error
        assert graph.edge_count() == 1

    def test_add_multiple_types(self):
        _seed_task("t1")
        _seed_chunk("c1")
        graph = _make_graph()
        graph.add_edge("t1", "c1", "cited_in_reasoning")
        graph.add_edge("t1", "c1", "builds_on")
        assert graph.edge_count() == 2

    def test_invalid_type_raises(self):
        _seed_task("t1")
        _seed_chunk("c1")
        graph = _make_graph()
        with pytest.raises(ValueError):
            graph.add_edge("t1", "c1", "made_up_type")


class TestQueryEdges:
    def test_tasks_referencing_chunk(self):
        _seed_task("t1")
        _seed_task("t2")
        _seed_chunk("c1")
        graph = _make_graph()
        graph.add_edge("t1", "c1")
        graph.add_edge("t2", "c1")
        tasks = graph.tasks_referencing_chunk("c1")
        assert set(tasks) == {"t1", "t2"}

    def test_chunks_referenced_by_active_tasks(self):
        _seed_task("t1", "in_progress")
        _seed_task("t2", "completed")
        _seed_chunk("c1")
        _seed_chunk("c2")
        graph = _make_graph()
        graph.add_edge("t1", "c1")
        graph.add_edge("t2", "c2")
        active_refs = graph.chunks_referenced_by_active_tasks()
        assert "c1" in active_refs
        assert "c2" not in active_refs

    def test_empty_graph(self):
        graph = _make_graph()
        assert graph.chunks_referenced_by_task("t_none") == set()
        assert graph.tasks_referencing_chunk("c_none") == []
        assert graph.chunks_referenced_by_active_tasks() == set()


class TestRemoveEdge:
    def test_remove_edge(self):
        _seed_task("t1")
        _seed_chunk("c1")
        graph = _make_graph()
        graph.add_edge("t1", "c1", "cited_in_reasoning")
        assert graph.edge_count() == 1
        graph.remove_edge("t1", "c1", "cited_in_reasoning")
        assert graph.edge_count() == 0

    def test_remove_nonexistent_edge_is_safe(self):
        graph = _make_graph()
        graph.remove_edge("t_ghost", "c_ghost", "cited_in_reasoning")
        assert graph.edge_count() == 0
