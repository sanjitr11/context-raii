"""context-raii: Task-scoped context management for Claude Code."""

from .storage import ensure_db, get_conn
from .task_registry import Task, TaskRegistry
from .context_tagger import ContextChunk, ContextTagger
from .reference_graph import ReferenceEdge, ReferenceGraph
from .eviction_engine import EvictionEngine, EvictionReport
from .compaction_advisor import CompactionAdvisor

__all__ = [
    "ensure_db",
    "get_conn",
    "Task",
    "TaskRegistry",
    "ContextChunk",
    "ContextTagger",
    "ReferenceEdge",
    "ReferenceGraph",
    "EvictionEngine",
    "EvictionReport",
    "CompactionAdvisor",
]
