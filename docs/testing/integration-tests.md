# Integration & benchmark suites

Phase 1 of [issue #800](https://github.com/D-sorganization/Maxwell-Daemon/issues/800).
This document covers how to run the integration and benchmark scaffolds
introduced under `tests/integration/` and `tests/benchmark/`. Subsequent
phases will extend these suites with regression baselines, larger E2E
matrices, and Locust-driven load profiles.

## What lives where

| Suite                | Path                                       | Purpose                                                                 |
| -------------------- | ------------------------------------------ | ----------------------------------------------------------------------- |
| API contract         | `tests/integration/test_api_contract.py`   | Pin the operator-facing `/api/health`, `/api/version`, `/api/status` and 404 error shapes against the Pydantic models in `maxwell_daemon/api/contract.py`. |
| End-to-end           | `tests/integration/test_end_to_end.py`     | Drive a task through the in-process daemon via `TestClient` to assert it reaches a terminal state (plus existing cost/metrics round-trips). |
| Benchmarks           | `tests/benchmark/test_benchmarks.py`       | Time `TaskStore.list_tasks` against a 500-row store. Skipped cleanly if `pytest-benchmark` is missing. |

All of these tests are hermetic: they rely on the in-memory `RecordingBackend`
fixture in `tests/conftest.py`, a temporary SQLite ledger, and a fresh
`Daemon` instance per test. No outbound network, no GitHub, no LLM calls.

## Running the suites

### Integration

```bash
pytest tests/integration/ -v
```

Expect a single fixture pattern: each test gets a fresh `Daemon` started on
its own event loop with `TestClient(create_app(daemon))` for HTTP traffic.
This mirrors `tests/unit/test_api.py` so contract-style tests can flow
freely between the unit and integration directories.

### Benchmarks

```bash
# Requires the optional pytest-benchmark plugin.
pip install 'pytest-benchmark>=4.0.0'

pytest tests/benchmark/test_benchmarks.py -v
```

If `pytest-benchmark` is not installed, the file is skipped via
`pytest.importorskip("pytest_benchmark")`, matching the optional-dependency
guidance in `CLAUDE.md` §3.

For comparing runs over time:

```bash
# Save a baseline
pytest tests/benchmark/test_benchmarks.py --benchmark-save=baseline

# Compare a new run against it
pytest tests/benchmark/test_benchmarks.py --benchmark-compare=baseline
```

## Adding to the suites (phase 2 and beyond)

When extending these scaffolds:

1. Reuse the `daemon` / `client` fixtures from
   `tests/integration/test_api_contract.py`. They already wire the
   `RecordingBackend`, isolated SQLite ledger, and per-test event loop.
2. Validate response shapes against the Pydantic models in
   `maxwell_daemon/api/contract.py` — never against literal dictionaries.
   This keeps the dashboard contract enforced in one place.
3. Treat the HTTP contract as **append-only** (`CLAUDE.md` §1). Adding a
   field is fine; renaming or removing one requires a `CONTRACT_VERSION`
   bump and coordination with `runner-dashboard`.
4. For new benchmarks, guard imports with
   `pytest.importorskip("pytest_benchmark")` so CI environments without
   the plugin still pass.

## Known limitations

- The benchmark suite is intentionally tiny in phase 1 — no budgets, no
  regression gates. That arrives once we collect baseline numbers across
  CI runners.
- The end-to-end smoke test only asserts a terminal state. Richer
  assertions (cost ledger entries, artifact creation, websocket events)
  live in the existing `TestEndToEnd` class in the same module.
- Locust load tests, multi-repo fleet integration, and websocket
  reconnect storms are deliberately out of scope here. Track them under
  follow-ups to issue #800.
