"""
Scenario: exploratory_abandon

Claude starts a task, reads many files during exploration, then the task is
never marked complete (user switches context, session ends, etc.).

This surfaces the "abandoned task" problem: all chunks are permanently pinned
to the in_progress task and can never be evicted.

Expected:
  eviction_rate         0%   (nothing evicts — task never completes)
  task_completion_rate  0%   (task left in_progress)
  refetch_rate          0%   (no evictions to trigger refetch)

This is a known limitation. The fix (future work) would be a timeout policy:
tasks inactive for >N minutes are automatically marked stale and their chunks
released.
"""

DESCRIPTION = "Task starts, reads 15 files, never completes (abandoned mid-session)"

EXPECTED = {
    "eviction_rate": (0.0, 0.05),   # essentially 0
    "task_completion_rate": (0.0, 0.05),
    "refetch_rate": (0.0, 0.0),
}


def run(h):
    h.task_create("explore", "Understand the authentication system")
    h.task_update("explore", "in_progress")

    # Wide exploration — reading many files
    modules = [
        "/src/auth.py", "/src/session.py", "/src/middleware.py",
        "/src/models/user.py", "/src/models/token.py",
        "/src/routes/login.py", "/src/routes/logout.py",
        "/src/utils/crypto.py", "/src/utils/jwt.py",
        "/src/config.py", "/src/constants.py",
        "/tests/test_auth.py", "/tests/conftest.py",
        "/docs/auth_design.md", "/README.md",
    ]
    for path in modules:
        h.read_file(path, f"# {path} contents\n" * 40)

    h.grep("def authenticate", "/src/", "auth.py:14: def authenticate()")
    h.bash("git log --oneline -10", "abc1234 Fix token expiry\ndef5678 Add MFA")

    # Session ends without task completion — intentionally no task_update("explore", "completed")
