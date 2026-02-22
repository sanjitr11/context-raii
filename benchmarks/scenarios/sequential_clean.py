"""
Scenario: sequential_clean

Baseline. Three independent tasks run sequentially, each completing cleanly
before the next starts. No shared chunks, no dependencies.

Expected:
  eviction_rate        ~100%  (all chunks owned by completed tasks)
  task_completion_rate  100%
  refetch_rate            0%
"""

DESCRIPTION = "3 independent tasks, each completes before the next starts"

EXPECTED = {
    "eviction_rate": (0.85, 1.0),
    "task_completion_rate": (1.0, 1.0),
    "refetch_rate": (0.0, 0.05),
}


def run(h):
    # Task 1: read two files, run tests, done
    h.task_create("t1", "Add login endpoint")
    h.task_update("t1", "in_progress")
    h.read_file("/src/auth.py", "def login(): pass\n" * 60)
    h.read_file("/src/models.py", "class User: pass\n" * 60)
    h.bash("pytest tests/auth/", "3 passed in 0.4s")
    h.edit_file("/src/auth.py")
    h.task_update("t1", "completed")

    # Task 2: grep, read, write, done
    h.task_create("t2", "Add signup form validation")
    h.task_update("t2", "in_progress")
    h.grep("validate", "/src/forms.py", "forms.py:12: def validate()")
    h.read_file("/src/forms.py", "def validate(): pass\n" * 80)
    h.edit_file("/src/forms.py")
    h.bash("npm run lint", "No issues found.")
    h.task_update("t2", "completed")

    # Task 3: explore + implement, done
    h.task_create("t3", "Fix session timeout bug")
    h.task_update("t3", "in_progress")
    h.read_file("/src/session.py", "SESSION_TTL = 3600\n" * 40)
    h.bash("grep -r 'SESSION_TTL' .", "session.py:1")
    h.edit_file("/src/session.py")
    h.bash("pytest tests/session/", "5 passed in 0.7s")
    h.task_update("t3", "completed")
