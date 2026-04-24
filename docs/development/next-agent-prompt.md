# Next Agent: Continue PR/Issue Resolution

## Current State (2026-04-24 ~2:00 PM PT)

### Already Merged to main
- **PR #653** — `codex/emergency-local-only-runners-20260424` ✅

### Auto-merge Queued (will merge once CI passes + reviews)
These PRs had merge conflicts resolved and auto-merge enabled:
- **#645** `fix/issue-632-guard-pyjwt-imports` — Guard optional PyJWT imports
- **#649** `feat/issue-477-mcp` — MCP support
- **#650** `chore/issue-497-supply-chain-hardening` — Supply-chain hardening
- **#651** `feat/issue-499-multimodal-attachments` — Multimodal attachments
- **#652** `feat/issue-601-token-budgeting` — Token budgeting
- **#654** `feat/issue-480-model-routing-complexity` — Complexity routing
- **#640** `feat/480-model-routing` — Model routing heuristic

### Still DIRTY (needs conflict resolution)
| PR | Branch | Blocker |
|---|---|---|
| **#630** | `feat/481-cost-estimator` | **Real content conflicts** with PR #653's merge to main: `api/server.py`, `core/backup.py`, `core/token_budget.py`. Needs interactive rebase + conflict resolution. |
| **#648** | `fix/test-suite-stabilization` | **15-file deep conflict** across `daemon/runner.py`, `core/ledger.py`, `core/task_store.py`, `logging.py`, `api/validation.py` and 7 test files. Cherry-pick fails. Needs author-level rebase. |

---

## Instructions for Next Agent

### 1. Check which auto-queued PRs merged
```bash
gh pr list --state open --json number,title,mergeStateStatus
```
If any show `mergeStateStatus: CLEAN`, they should merge automatically. If still `BLOCKED`/`UNKNOWN`, wait for CI.

### 2. Re-resolve PR #630 (cost estimator)
PR #653 merged to main and introduced new conflicts. Fetch latest:
```bash
git fetch origin feat/481-cost-estimator
git checkout -B feat/481-cost-estimator origin/feat/481-cost-estimator
git merge origin/main
```
Resolve conflicts in:
- `maxwell_daemon/api/server.py`
- `maxwell_daemon/core/backup.py`
- `maxwell_daemon/core/token_budget.py`

Strategy: The cost estimator PR adds `core/cost_estimator.py` and modifies budget/metrics. PR #653 added local-runner guards. Prefer **incoming** (cost estimator) for new feature code, **main** for structural changes. After resolving, push and enable auto-merge.

### 3. PR #648 (test stabilization) — Requires Deep Work
This PR is the most complex. The branch has 4 commits:
1. `dff085f` — fix: stabilize test suite and resolve flaky mocks
2. `8792402` — chore: enforce 85% test coverage in CI
3. `68ae951` — test: add missing coverage for builtins
4. `093b1f0` — feat: token optimization and aggressive compression

These touch heavily refactored files. Options:
- **Option A**: Cherry-pick only the test coverage commits (`8792402`, `68ae951`) which may apply cleaner
- **Option B**: Create a new branch from main and manually port the changes
- **Option C**: Close PR #648 and open smaller focused PRs for each commit

### 4. Create Issues for Blocked PRs
If PR #648 cannot be resolved quickly:
```bash
gh issue create --title "PR #648 needs rebase after daemon/runner.py refactoring" \
  --body "The fix/test-suite-stabilization branch has 15-file conflicts with main due to recent changes in daemon/runner.py, core/ledger.py, core/task_store.py, logging.py, and api/validation.py. The author needs to rebase onto current main."
```

### 5. Rate Limit Compliance
- **ALWAYS** check rate limits first: `gh api rate_limit`
- **NEVER** use `gh pr list --json` in loops — it uses GraphQL
- **ALWAYS** use `git branch -a` (local) for branch discovery
- **REST over GraphQL** for status checks: `gh api repos/OWNER/REPO/pulls/NUMBER` not `gh pr view --json`

---

## Key Files Changed Recently on main
- `maxwell_daemon/metrics.py` — `__all__` sorted (RUF022 fix)
- `test_diff.py` — import order fixed (I001 fix)
- `.github/workflows/ci.yml` — new local-only runner guard
- `scripts/check_local_only_workflows.py` — new file
- `maxwell_daemon/core/backup.py` — updated by PR #653
- `maxwell_daemon/api/server.py` — updated by PR #653

---

## GitHub API Quotas (from AGENTS.md)
| API | Quota | Note |
|---|---|---|
| REST | 5,000/hr | Safe for single lookups |
| GraphQL | 5,000/hr | **Avoid** — shared across fleet |

If rate limit hit: halt all API calls, use local git only, check again after 60+ minutes.