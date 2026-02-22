"""
Scenario: cross_cutting_refactor

Task A reads 10 files while in_progress. Task B then starts and edits 6 of
those same files. Write-invalidation should immediately mark task A's reads
of the edited files as evictable — even though task A is still active.

This tests Weakness 1 Solution A: write-invalidation.

Expected:
  eviction_rate        >=60%  (write-invalidation fires mid-task)
  task_completion_rate  100%
  refetch_rate           ~0%  (no re-reads after eviction in this scenario)

Without write-invalidation, A's reads would be 0% evictable until A completes.
With write-invalidation, 6/10 of A's reads evict immediately on B's edits.
"""

DESCRIPTION = "Task A reads 10 files; Task B edits 6 of them (write-invalidation)"

EXPECTED = {
    "eviction_rate": (0.60, 1.0),
    "task_completion_rate": (1.0, 1.0),
    "refetch_rate": (0.0, 0.10),
}

FILES = [f"/src/module_{i}.py" for i in range(10)]
EDITED_FILES = FILES[4:]   # B edits the last 6 files that A read


def run(h):
    # Task A: reads all 10 files, still in_progress
    h.task_create("ta", "Audit entire codebase")
    h.task_update("ta", "in_progress")
    for f in FILES:
        h.read_file(f, f"# content of {f}\n" * 50)
    h.bash("wc -l /src/*.py", "1200 total")

    # Task B starts (now current active task)
    h.task_create("tb", "Refactor modules 4-9")
    h.task_update("tb", "in_progress")

    # B edits 6 of the files A read — write-invalidation should fire
    for f in EDITED_FILES:
        h.edit_file(f, old_string="# content", new_string="# refactored content")

    h.bash("pytest tests/", "42 passed in 2.1s")

    # Task A completes — its 4 non-invalidated reads should now evict
    h.task_update("ta", "completed")

    # Task B completes — B's own chunks evict
    h.task_update("tb", "completed")
