"""
Scenario: long_chain

5 sequential tasks where each declares dependsOn the previous. Tests that
declared dependencies produce correct rolling eviction: task N's chunks are
pinned until task N+1 (which depends on N) also completes.

Timeline:
  All 5 tasks created upfront (all pending, dependency chain declared).
  Task 1 completes → chunks pinned (task 2 is pending, depends on task 1)
  Task 2 completes → task 1's chunks evict, task 2's chunks pinned (task 3 pending)
  Task 3 completes → task 2's chunks evict, task 3's chunks pinned (task 4 pending)
  Task 4 completes → task 3's chunks evict, task 4's chunks pinned (task 5 pending)
  Task 5 completes → task 4's chunks evict, task 5's chunks evict (no more dependents)

Expected:
  eviction_rate        ~100%  (all tasks eventually complete)
  task_completion_rate  100%
  refetch_rate            0%

The key observable: eviction is deferred — chunks don't evict immediately
on task completion, only after the next task in the chain also completes.
"""

DESCRIPTION = "5-task dependency chain — rolling eviction as each task completes"

EXPECTED = {
    "eviction_rate": (0.85, 1.0),
    "task_completion_rate": (1.0, 1.0),
    "refetch_rate": (0.0, 0.05),
}

TASK_IDS = ["chain-1", "chain-2", "chain-3", "chain-4", "chain-5"]


def run(h):
    # Create all tasks upfront with dependency chain declared
    h.task_create(TASK_IDS[0], "Design data model")
    for i in range(1, len(TASK_IDS)):
        h.task_create(
            TASK_IDS[i],
            f"Build on chain-{i}",
            depends_on=[TASK_IDS[i - 1]],
        )

    for i, task_id in enumerate(TASK_IDS):
        h.task_update(task_id, "in_progress")
        # Each task reads 3 files unique to it
        for j in range(3):
            h.read_file(f"/src/layer_{i}_{j}.py", f"# layer {i} module {j}\n" * 50)
        h.bash(f"pytest tests/layer_{i}/", f"{3 + i} passed")
        h.edit_file(f"/src/layer_{i}_main.py")
        h.task_update(task_id, "completed")
