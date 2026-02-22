"""
Scenario: parallel_tasks

Two tasks are in_progress simultaneously with interleaved tool calls.
Some files are read by only one task; one file is read by both (shared).

Tests that ref-counting on shared chunks works correctly:
  - When task A completes, only A's exclusive chunks evict
  - The shared chunk stays (task B still active, its read supersedes A's)
  - When task B completes, B's chunks (including the shared file's newer read) evict

Expected:
  eviction_rate        ~85%  (phased eviction: A's exclusive evict first, then B's)
  task_completion_rate  100%
  refetch_rate           ~0%
"""

DESCRIPTION = "Two tasks in parallel, shared file read by both — phased eviction"

EXPECTED = {
    "eviction_rate": (0.75, 1.0),
    "task_completion_rate": (1.0, 1.0),
    "refetch_rate": (0.0, 0.10),
}

A_FILES = [f"/src/feature_a_{i}.py" for i in range(5)]
B_FILES = [f"/src/feature_b_{i}.py" for i in range(5)]
SHARED_FILE = "/src/shared_utils.py"


def run(h):
    # Both tasks created upfront
    h.task_create("ta", "Implement feature A")
    h.task_create("tb", "Implement feature B")

    # Task A goes active, reads its files + the shared file
    h.task_update("ta", "in_progress")
    for f in A_FILES:
        h.read_file(f, f"# feature A module\n" * 50)
    h.read_file(SHARED_FILE, "def shared_util(): pass\n" * 60)
    h.bash("pytest tests/feature_a/", "8 passed")

    # Task B goes active (now current_active = B), reads its files + shared file
    # B's read of shared_utils supersedes A's read (same path, newer chunk)
    h.task_update("tb", "in_progress")
    for f in B_FILES:
        h.read_file(f, f"# feature B module\n" * 50)
    h.read_file(SHARED_FILE, "def shared_util(): pass\n" * 60)
    h.bash("pytest tests/feature_b/", "6 passed")

    # Task A completes:
    #   - A's 5 exclusive reads → evictable (task A done, no active refs)
    #   - A's shared_utils read → evictable (superseded by B's read, A done)
    #   - B's chunks → NOT evictable (B still active)
    h.task_update("ta", "completed")

    # Task B completes:
    #   - B's 5 exclusive reads + shared read + bash → all evictable
    h.task_update("tb", "completed")
