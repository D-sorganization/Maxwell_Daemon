# CLAUDE.md — Maxwell-Daemon

> Companion to `AGENTS.md`.  This file contains Claude-specific context,
> architectural guard-rails, and common pitfalls when editing this repo.

---

## Quick Orientation

`Maxwell-Daemon` is an **autonomous AI control plane** written in Python
(FastAPI + vanilla-JS UI).  It orchestrates agent tasks, manages a
state-machine-driven pipeline, and exposes a stable HTTP API consumed by
`runner-dashboard`.

Key directories:

```
maxwell_daemon/
├── api/          # FastAPI routes, WebSocket events, static UI
├── backends/     # LLM adapters (Anthropic, OpenAI, Ollama, …)
├── cli/          # Typer CLI entry-points
├── core/         # TaskStore, cost ledger, event bus
├── daemon/       # Runner, task lifecycle, state machine
├── fleet/        # Multi-repo fleet manifest + dispatcher
├── gh/           # GitHub API client
├── metrics.py    # Prometheus instrumentation
└── ssh/          # SSH key store + session pool
```

---

## When Editing This Repo

### 1. Keep the HTTP Contract Stable

The dashboard's **Maxwell tab** calls these endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/version` | Semver + contract version |
| GET | `/api/health` | Liveness, gate state |
| GET | `/api/status` | Pipeline state, active task |
| POST | `/api/dispatch` | Submit signed task envelope |
| POST | `/api/control/{pause,resume,abort}` | Privileged control |

**Rule:** append-only.  Add new endpoints freely; never change existing
request/response shapes without bumping the major version advertised at
`GET /api/version`.

### 2. Logging, Never Print

The project uses `structlog` (see `maxwell_daemon/logging.py`).  Every
`print()` you add is a regression.  If you need debug output during
development, use:

```python
import structlog
logger = structlog.get_logger()
logger.debug("…")
```

CI enforces this via `ruff` (rule `T201`).

### 3. Optional Dependencies

`asyncssh` and `PyJWT` are **optional**.  Production code must use lazy
imports; tests must guard with `pytest.importorskip()`:

```python
# production
asyncssh = None
def _ensure_asyncssh():
    global asyncssh
    if asyncssh is None:
        import asyncssh
    return asyncssh

# test
pytest.importorskip("asyncssh")
```

### 4. SQLite Cost Ledger

Costs are tracked in a WAL-mode SQLite file.  Do **not** introduce an ORM
abstraction without a migration plan.  The ledger is append-only for audit
integrity.

### 5. State Machine Idempotence

The daemon loop (`maxwell_daemon/daemon/runner.py`) must be safe to
restart at any point.  All state transitions should be recoverable from
the `TaskStore`.  Never hold un-persisted state in memory across
iterations.

---

## Common Pitfalls

| Pitfall | Why it happens | How to avoid |
|---------|---------------|--------------|
| Breaking dashboard integration | Changing `/api/status` shape | Add fields, don't rename/remove |
| CI failure on optional import | Missing `importorskip` in tests | Always guard optional deps |
| Type-check failure | Missing `-> None` or `Any` return hints | Run `mypy --strict` locally |
| Security flag from bandit | Using `subprocess` with `shell=True` | Pass list args, never `shell=True` |
| File > 500 KB flagged | Committing large binary artifacts | Use `git-lfs` or external storage |

---

## Testing a Change Locally

```bash
# lint + format + type-check
ruff check . && ruff format --check . && mypy --strict maxwell_daemon/

# unit tests
pytest tests/unit/

# integration tests (needs local SQLite, no external services)
pytest tests/integration/

# full security scan
bandit -r maxwell_daemon -c pyproject.toml
```

---

## Cross-Repo Boundaries

| Traffic direction | Allowed? |
|-------------------|----------|
| `runner-dashboard` → `Maxwell-Daemon` | ✅ (HTTP API) |
| `Maxwell-Daemon` → `runner-dashboard` | ❌ never |
| `Maxwell-Daemon` → `Repository_Management` | ❌ never |

This one-way rule keeps the dependency graph acyclic and lets the daemon
stay reusable from any caller.

---

## Reference

- `AGENTS.md` — full agent directives, fleet API hygiene, coding standards
- `SPEC.md` — repository specification (API schema, event system, fleet manifest)
- `docs/adr/` — architecture decision records
- `docs/operations/observability.md` — metrics, logging, alerting