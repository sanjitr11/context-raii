"""
Scenario: exploratory_abandon

Claude starts a task, reads many files during exploration, then the task is
never marked complete (user switches context, session ends, etc.).

After the abandoned-task timeout (Rule 5), an in_progress task with 50+
context_chunks created after it is automatically transitioned to 'abandoned'
and treated as complete for eviction. Its chunks are freed.

The timeout fires when the PreCompact hook runs (or when any other task
completes). This scenario simulates a compaction event at the end via
h.pre_compact() to trigger the eviction engine.

Expected:
  eviction_rate         high  (chunks freed via auto-abandon)
  task_completion_rate  0%    (task is 'abandoned', not 'completed')
  refetch_rate          0%    (no re-reads of evicted files in this scenario)
"""

DESCRIPTION = "Task starts, reads 55 files, never completes — auto-abandoned at 50-call threshold"

EXPECTED = {
    "eviction_rate": (0.75, 1.0),      # chunks freed via auto-abandon
    "task_completion_rate": (0.0, 0.05),  # task is abandoned, not completed
    "refetch_rate": (0.0, 0.0),
}


def run(h):
    h.task_create("explore", "Understand the authentication system")
    h.task_update("explore", "in_progress")

    # Wide exploration — reading many files (55 > 50-call threshold)
    modules = [
        "/src/auth.py", "/src/session.py", "/src/middleware.py",
        "/src/models/user.py", "/src/models/token.py",
        "/src/routes/login.py", "/src/routes/logout.py",
        "/src/utils/crypto.py", "/src/utils/jwt.py",
        "/src/config.py", "/src/constants.py",
        "/tests/test_auth.py", "/tests/conftest.py",
        "/docs/auth_design.md", "/README.md",
        "/src/permissions.py", "/src/roles.py", "/src/groups.py",
        "/src/oauth/google.py", "/src/oauth/github.py",
        "/src/oauth/base.py", "/src/oauth/utils.py",
        "/src/db/models.py", "/src/db/migrations.py", "/src/db/connection.py",
        "/src/api/v1/auth.py", "/src/api/v1/users.py", "/src/api/v1/tokens.py",
        "/src/api/v2/auth.py", "/src/api/v2/users.py",
        "/src/cache/redis.py", "/src/cache/local.py",
        "/src/email/sender.py", "/src/email/templates.py",
        "/src/audit/log.py", "/src/audit/events.py",
        "/src/security/rate_limit.py", "/src/security/csrf.py",
        "/src/security/headers.py", "/src/security/validator.py",
        "/src/cli/admin.py", "/src/cli/manage.py",
        "/src/tasks/cleanup.py", "/src/tasks/notifications.py",
        "/tests/test_session.py", "/tests/test_middleware.py",
        "/tests/test_permissions.py", "/tests/test_oauth.py",
        "/tests/integration/test_login.py",
        "/tests/integration/test_logout.py",
        "/docs/security_model.md", "/docs/api_reference.md",
        "/docs/deployment.md",
    ]
    for path in modules:
        h.read_file(path, f"# {path} contents\n" * 40)

    h.grep("def authenticate", "/src/", "auth.py:14: def authenticate()")
    h.bash("git log --oneline -10", "abc1234 Fix token expiry\ndef5678 Add MFA")

    # Session ends without task completion — no task_update("explore", "completed").
    # Simulate compaction firing (which triggers the eviction engine and abandon check).
    h.pre_compact()
