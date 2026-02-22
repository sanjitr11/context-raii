"""
SQLite persistence layer for context-raii.
DB lives at ~/.claude/raii/state.db and survives across hook invocations and sessions.
"""

import os
import sqlite3
import json
from pathlib import Path
from contextlib import contextmanager
from typing import Iterator

# Allow test harnesses to redirect the DB via env var
_db_dir_override = os.environ.get("RAII_DB_DIR")
DB_DIR = Path(_db_dir_override) if _db_dir_override else Path.home() / ".claude" / "raii"
DB_PATH = DB_DIR / "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    subject         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    parent_id       TEXT,
    created_at      TEXT NOT NULL,
    completed_at    TEXT,
    metadata        TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS context_chunks (
    id              TEXT PRIMARY KEY,
    tool_name       TEXT NOT NULL,
    tool_input      TEXT DEFAULT '{}',
    is_refetchable  INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'fresh',
    size_tokens     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    session_id      TEXT,
    content_hash    TEXT
);

CREATE TABLE IF NOT EXISTS reference_edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_task_id  TEXT NOT NULL,
    target_chunk_id TEXT NOT NULL,
    reference_type  TEXT NOT NULL DEFAULT 'cited_in_reasoning',
    created_at      TEXT NOT NULL,
    UNIQUE(source_task_id, target_chunk_id, reference_type),
    FOREIGN KEY(source_task_id) REFERENCES tasks(id),
    FOREIGN KEY(target_chunk_id) REFERENCES context_chunks(id)
);

CREATE TABLE IF NOT EXISTS task_chunks (
    task_id     TEXT NOT NULL,
    chunk_id    TEXT NOT NULL,
    tagged_at   TEXT NOT NULL,
    PRIMARY KEY(task_id, chunk_id),
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(chunk_id) REFERENCES context_chunks(id)
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    dependent_task_id  TEXT NOT NULL,
    dependency_task_id TEXT NOT NULL,
    PRIMARY KEY (dependent_task_id, dependency_task_id),
    FOREIGN KEY(dependent_task_id)  REFERENCES tasks(id),
    FOREIGN KEY(dependency_task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_chunks_status ON context_chunks(status);
CREATE INDEX IF NOT EXISTS idx_edges_task ON reference_edges(source_task_id);
CREATE INDEX IF NOT EXISTS idx_edges_chunk ON reference_edges(target_chunk_id);
CREATE INDEX IF NOT EXISTS idx_task_chunks_task ON task_chunks(task_id);
CREATE INDEX IF NOT EXISTS idx_task_chunks_chunk ON task_chunks(chunk_id);
CREATE INDEX IF NOT EXISTS idx_deps_dependent ON task_dependencies(dependent_task_id);
CREATE INDEX IF NOT EXISTS idx_deps_dependency ON task_dependencies(dependency_task_id);
"""

# Migrations for columns added after initial schema creation.
# SQLite has no ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so we try/except.
_MIGRATIONS = [
    "ALTER TABLE context_chunks ADD COLUMN status_changed_at TEXT",
]


def ensure_db() -> None:
    """Create the DB directory and initialize schema if needed."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        for migration in _MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Context manager yielding a SQLite connection with row_factory set."""
    ensure_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def serialize(obj) -> str:
    return json.dumps(obj, default=str)


def deserialize(s: str):
    if s is None:
        return {}
    return json.loads(s)
