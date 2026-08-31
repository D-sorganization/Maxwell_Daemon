# Issue: PR #648 Needs Rebase After daemon/runner.py Refactoring

**Status**: Blocked — author-level rebase required
**Rate Limit**: GraphQL exceeded — do NOT make GitHub API calls until reset

## Problem

The `fix/test-suite-stabilization` branch has 15-file conflicts with main due to recent changes in:
- `daemon/runner.py`
- `core/ledger.py`
- `core/task_store.py`
- `logging.py`
- `api/validation.py`
- `config/models.py`
- `api/server.py`
- `core/action_service.py`
- `gh/context.py`
- `tests/conftest.py`
- And 7 test files

## Branch Commits

1. `dff085f` — fix: stabilize test suite and resolve flaky mocks
2. `8792402` — chore: enforce 85% test coverage in CI
3. `68ae951` — test: add missing coverage for builtins and fix end_to_end validation
4. `093b1f0` — feat: token optimization and aggressive compression

## Recommendation

Split into smaller focused PRs:

**PR A (test stabilization)**: Cherry-pick commits 1-3 onto a fresh branch from main
```bash
git checkout -b fix/test-stabilization-v2 origin/main
git cherry-pick dff085f
git cherry-pick 8792402
git cherry-pick 68ae951
# Resolve any remaining conflicts manually
```

**PR B (token optimization)**: Cherry-pick commit 4 separately
```bash
git checkout -b feat/token-optimization-v2 origin/main
git cherry-pick 093b1f0
# Resolve conflicts in daemon/runner.py, metrics.py, etc.
```

## Rate Limit Recovery

Wait until GraphQL rate limit resets before creating issues/PRs on GitHub.
Check status: `gh api rate_limit` (uses REST, safe)
