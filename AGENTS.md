# AGENTS.md

## 🤖 Agent Personas & Directives

**Audience:** This document is the authoritative guide for AI agents working in this repository.

**Core Mission:**

- Write high-quality, maintainable, and secure code.
- Adhere strictly to the project's architectural and stylistic standards.
- Act as a responsible pair programmer, always verifying assumptions and testing changes.

---

## 🗺️ Sibling repos & boundaries (read first)

`Maxwell-Daemon` is the **autonomous AI control plane** in a three-repo
fleet. The cross-repo contract is documented canonically in
[`Repository_Management/docs/sibling-repos.md`](https://github.com/D-sorganization/Repository_Management/blob/main/docs/sibling-repos.md).

| Repo                     | Role                                                         |
| ------------------------ | ------------------------------------------------------------ |
| [`Repository_Management`](https://github.com/D-sorganization/Repository_Management) | Fleet orchestrator (CI workflows, skills, templates, agent coordination). |
| [`runner-dashboard`](https://github.com/D-sorganization/runner-dashboard) | Operator console; calls Maxwell-Daemon's HTTP API from its Maxwell tab. |
| `Maxwell-Daemon` (here)  | Strategist / Implementer / Crucible state machine, ExecutionSandbox, BYO-CLI runtime, gate-aware `/ui/`. |

**Rule that keeps the graph acyclic:** Maxwell-Daemon **never calls back**
into `runner-dashboard` or `Repository_Management`. Cross-repo traffic is
always *into* the daemon — never out. This is what lets the daemon stay
reusable from any caller (CLI, dashboard, future clients).

**HTTP surface this repo must keep stable** (consumed by the dashboard's
Maxwell tab — see the sibling-repos doc for full schema):

| Method | Path                                | Purpose                                       |
| ------ | ----------------------------------- | --------------------------------------------- |
| GET    | `/api/version`                      | Daemon semver + contract version              |
| GET    | `/api/health`                       | Liveness, gate state, current focus           |
| GET    | `/api/status`                       | Pipeline state, active task, gates, sandbox   |
| GET    | `/api/tasks`                        | Recent task history (paginated)               |
| GET    | `/api/tasks/{id}`                   | One task incl. transcript + artifacts         |
| POST   | `/api/dispatch`                     | Submit a signed task envelope                 |
| POST   | `/api/control/{pause,resume,abort}` | Pipeline control (privileged)                 |

This contract is **append-only**: add endpoints freely; never break existing
ones without a major-version bump advertised at `GET /api/version`.

**Routing rule:** dashboard tabs / `/api/*` endpoints in `runner-dashboard` →
that repo; fleet workflows / skills / templates → `Repository_Management`;
pipeline state, gates, sandbox, BYO-CLI → here.

---

## 🛡️ Safety & Security (CRITICAL)

1. **Secrets Management**:
   - **NEVER** commit API keys, passwords, tokens, or database connection strings.
   - Use environment variables or the config file (`~/.config/maxwell-daemon/config.toml`).
   - Create `.env.example` templates for required environment variables.
2. **Code Review**:
   - Review all generated code for security vulnerabilities (SQL injection, unsafe file I/O, etc.).
   - Do not accept code you do not understand.
3. **Data Protection**:
   - Do not commit large binary files (>50MB) or personal data.

---

<!-- BEGIN FLEET-MANAGED: network-api-hygiene -->

## 🛑 NETWORK & API HYGIENE (CRITICAL)

> This section is managed centrally by Repository_Management and synced fleet-wide.
> Do NOT edit it directly in individual repositories — edit the source in Repository_Management/AGENTS.md.

### GitHub API Quotas

| API Type                  | Quota        | Consumed By                                                        |
| ------------------------- | ------------ | ------------------------------------------------------------------ |
| REST (`gh api repos/...`) | 5,000 req/hr | Safe for polling                                                   |
| GraphQL                   | 5,000 req/hr | `gh pr list --json`, `gh pr checks`, `gh pr create`, `gh pr merge` |

GraphQL and REST have **separate** quotas. Exhausting GraphQL blocks PR creation and merging fleet-wide for an entire hour.

### Mandatory Rules

- **NO MASS POLLING**: Agents MUST NEVER use `gh pr list`, `gh issue list`, or arbitrary REST/GraphQL loops in a bulk manner to "scan" or "sweep" the repository fleet. Single, scoped repository lookups are allowed when needed (e.g., checking if a specific PR exists).
- **LOCAL FIRST**: Rely on local `.md` files, previously generated `issues.json` artifacts, or user assistance to find task context — do not query GitHub to discover what to work on.
- **NO PARALLELIZED GITHUB CLI**: Never write or execute scripts that loop over multiple repositories performing `gh` operations (automated PR merge scripts, fleet-wide status sweeps, etc.).
- **NO TIGHT POLLING LOOPS**: Never implement `while true; do gh pr checks $PR; sleep 30; done` patterns. Each iteration of such a loop costs 1–3 GraphQL calls; at 30-second intervals that drains the 5,000/hr quota in under 3 hours.
  - ❌ `while true; do gh pr checks; sleep 30; done`
  - ✅ `gh run watch <run-id>` — streams CI events without polling
  - ✅ Check status once at natural work breakpoints (after completing other tasks)
- **BATCHING**: If remote information is absolutely necessary, use a single focused query — not a loop of queries.
- **REST OVER GRAPHQL FOR CI STATUS**: Use REST endpoints for CI polling; they don't consume the GraphQL quota.
  - ❌ `gh pr checks <N>` (GraphQL)
  - ✅ `gh api repos/OWNER/REPO/actions/runs` (REST)
  - ✅ `gh api repos/OWNER/REPO/actions/jobs/<id>/logs` (REST)
- **STOP MONITORS IMMEDIATELY**: When using background monitor tasks, call `TaskStop <id>` the moment the monitored condition is satisfied. Do not leave monitors running "just in case."
- **LONG POLLING INTERVALS**: Background monitors must use ≥270-second intervals (keeps the prompt cache warm). Default to 1200–1800 s for idle monitoring. Never chain short sleeps to work around the 60-second minimum.
- **SILENT FAILURES**: If an API rate limit is hit, HALT NETWORK ACTIVITY IMMEDIATELY. Do not write retry-loops that further exhaust the quota. Alert the user and pivot to local work.

### Checking Rate Limit Status

```bash
gh api rate_limit | python3 -c "
import json, sys, datetime
d = json.load(sys.stdin)['resources']
for k in ['core', 'graphql']:
    r = d[k]
    reset = datetime.datetime.fromtimestamp(r['reset']).strftime('%H:%M:%S')
    print(f'{k}: {r["remaining"]}/{r["limit"]} remaining — resets {reset}')
"
```

<!-- END FLEET-MANAGED: network-api-hygiene -->

---

## 🐍 Python Coding Standards

### 1. Code Quality & Style

- **Logging vs. Print**: Use `structlog` (configured in `maxwell_daemon/logging.py`). Never use `print()`.
- **Imports**: No wildcard imports (`from module import *`). Explicitly import required classes/functions.
- **Exception Handling**: No bare `except:`. Catch specific exceptions or at least `except Exception:`.
- **Type Hinting**: Required on all function signatures (`mypy --strict` is enforced in CI).
- **Line length**: 100 characters (configured in `pyproject.toml`).

### 2. Project Structure

```
maxwell_daemon/
├── api/          # FastAPI app, WebSocket events, UI static files
├── audit.py      # Append-only JSONL audit log with SHA-256 chaining
├── auth.py       # JWT/RBAC — Role enum, JWTConfig, require_role()
├── backends/     # LLM backend adapters (Anthropic, OpenAI, Ollama, etc.)
├── cli/          # Typer CLI entry-points
├── core/         # TaskStore, event bus, cost ledger
├── daemon/       # Daemon orchestrator, Runner, Task model
├── director/     # Issue-to-plan reconciler
├── fleet/        # Fleet manifest, dispatcher, remote client
├── gh/           # GitHub API client
├── metrics.py    # Prometheus metrics
├── ssh/          # SSH key store, session pool (optional: asyncssh)
└── tools/        # Built-in agent tools
```

### 3. Testing

- All tests live in `tests/unit/` or `tests/integration/`.
- Use `pytest-asyncio` with `asyncio_mode = "auto"` (set in `pyproject.toml`).
- Optional dependencies (asyncssh, PyJWT) must be guarded with `pytest.importorskip()` at module level.
- Run: `pytest tests/` — coverage report is generated automatically.
- CI runs Python 3.10, 3.11, and 3.12.

### 4. CI Gates (all must pass before merge)

| Gate | Tool |
|------|------|
| Lint | `ruff check .` |
| Format | `ruff format --check .` |
| Type check | `mypy --strict maxwell_daemon/` |
| Security | `bandit -r maxwell_daemon -c pyproject.toml` |
| Tests | `pytest tests/` (py3.10, py3.11, py3.12) |
| File budget | No file >500 KB |

---

## 🔀 Git & Branch Conventions

- **Branch naming**: `fix/issue-N-description` or `feat/issue-N-description` for human work; `bot/...` for automated branches.
- **Commit messages**: Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`).
- **PRs**: Always reference the closing issue (`Closes #N`). Squash-merge into main.
- **Do NOT force-push to main**.

---

## 🏗️ Architecture Notes

- **FastAPI + vanilla JS**: The UI (`maxwell_daemon/api/ui/`) is a plain JS SPA — no build step required. Do not introduce npm dependencies.
- **Canonical desktop shell**: Treat the browser-served `/ui/` control plane as the single shipped operator UI. Electron may wrap it, but do not reintroduce the retired PyQt desktop stub.
- **SQLite cost ledger**: Costs are tracked in a WAL-mode SQLite file. Never replace it with an ORM-based abstraction without a migration.
- **WebSocket events**: Agent progress is streamed via `GET /api/v1/events` (SSE) and `WS /api/v1/ws`. Always test event propagation when modifying the daemon loop.
- **Fleet manifest**: `fleet.yaml` defines repos and agent slots. Validate with `maxwell_daemon/fleet/config.py` before modifying the schema.
- **Optional deps**: `asyncssh` (for SSH), `PyJWT` (for auth) are optional. Guard usage with `importorskip` in tests and lazy imports in production code.

---

## 🧠 LLM Operations & Best Practices

### Model Selection
- **Haiku**: Use for formatting, parsing, syntax checks, or summarizing existing context. (Cheap, fast).
- **Sonnet**: Use for standard code generation and routine feature implementation. (Balanced).
- **Opus**: Use STRICTLY for complex architectural planning, difficult refactors, and critical debugging where deep reasoning is needed. (Expensive).

### Critic Verdicts
- **Severities**: Critical (blocks merge), Warning (should fix, but non-blocking), Info (suggestions).
- Always address Critical findings before re-requesting a gate check.

### Memory & Token Accounting
- Tasks run within a constrained token budget. Always prefer compressing context (using `MAXWELL_AGGRESSIVE_COMPRESSION`) over passing massive raw files.
- Ensure that repetitive tasks don't bloat the history; summarize past findings.

### Coding Practices
- **Idempotence**: Scripts and setup functions must be safe to run multiple times.
- **Error Messages**: Write actionable error messages (e.g. "Failed to bind port 8080 (already in use). Did you leave another daemon running?").
- **Naming**: Use clear, descriptive variable names. Avoid cryptic abbreviations.

---

## 📂 Repository Decluttering

All development documentation (summaries, plans, analysis) MUST go in `docs/development/`. Do NOT create `.md` files in the repo root unless they are critical project-wide files (README, AGENTS, CHANGELOG).

<!-- BEGIN FLEET-MANAGED: reasoning-engagement -->

## 🧠 Reasoning & Engagement

> This section is managed centrally by Repository_Management and synced fleet-wide.
> Do NOT edit it directly in individual repositories — edit the source in Repository_Management/AGENTS.md.

These rules govern *how* you engage with a task before and during implementation. They exist because LLM agents tend to pick an interpretation silently, overcomplicate the solution, and edit code they were not asked to touch. Each rule directly counteracts one of those failure modes.

- **Surface ambiguity. Do not guess silently.** If the request has more than one plausible interpretation, list the options and ask before implementing. Picking one and running with it is the single most common cause of rework in this fleet.
- **Push back on overcomplication.** If a simpler approach would satisfy the request, say so before you build the complicated one. Do not implement bloated 1000-line constructions when 100 would do. The senior-engineer test: would they call this overcomplicated? If yes, simplify.
- **Stay surgical.** Every changed line must trace directly to the user's request. Do not "improve" adjacent code, comments, formatting, or imports. Do not refactor things that are not broken. Match existing style even if you would do it differently.
- **Spotted ≠ fix.** If you notice unrelated dead code, latent bugs, or stylistic problems while working, *mention them in the PR body or as a follow-up issue* — do not fix them in the same PR. (The `mcp__ccd_session__spawn_task` tool is the right channel when working interactively.)
- **Clean up only your own orphans.** If your changes leave imports, variables, or functions newly unused, remove them. Do not delete pre-existing dead code unless the task asked for it.
- **State a verifiable success criterion before coding.** For a bug fix, that's a failing test that reproduces it (RED → GREEN, see TDD section below). For a feature, the explicit check that says "done." "Make it work" is not a success criterion.

**The diff test:** every line in your final diff should answer "this is here because the user asked for X." If you cannot answer that for a given line, remove it.

<!-- END FLEET-MANAGED: reasoning-engagement -->
---

## Specification

This repository's specification is defined in `SPEC.md` at the repo root (if present).
Read it before making significant changes to the API, fleet manifest schema, or event system.


## Closing issues — non-negotiable rule

NEVER close a feature or bug issue without one of:

1. A merged PR that implements the acceptance criteria (use `Closes #N` in the PR body or title), OR
2. An explicit `wontfix`, `roadmap`, `duplicate`, `invalid`, or `not-planned` label.

The **Verify-Issue-Closure** workflow will automatically reopen any issue closed without evidence. Do not work around it.

When implementing an issue:
- Write or update tests FIRST (TDD: red → green → refactor)
- Add Design-by-Contract preconditions/postconditions where it clarifies invariants
- Respect Law of Demeter — don’t reach through three layers
- Don’t duplicate code (DRY)
- Run tests locally before pushing
- If you can’t fully implement, leave the issue open and post a status comment

### How to close issues properly

| Method | Example |
|--------|---------|
| Closing keyword in PR body | `Closes #1234` or `Fixes #5678` |
| Closing keyword in PR title | `fix: resolve login crash (#1234)` |
| Exempt label | Apply `wontfix`, `roadmap`, `duplicate`, `invalid`, or `not-planned` |
| Bot + auto-generated label | Only for auto-generated issues closed by bots |

The workflow checks the PR timeline for cross-referenced merged PRs with closing keywords. If none are found and no exempt label is present, the issue is reopened with an explanatory comment.
