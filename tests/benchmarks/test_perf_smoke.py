"""Phase-1 performance benchmark smoke tests.

These benchmarks intentionally cover three different layers of the daemon
so a regression anywhere in the hot path shows up at least once:

* ``GET /api/status`` — HTTP layer p50 latency (representative of the
  read traffic the dashboard generates).
* ``DispatchRequest`` validation throughput — Pydantic envelope shape
  validation (called once per ``POST /api/dispatch``).
* ``TaskStore.save`` throughput — SQLite write path used on every
  enqueued task.

They are *opt-in*: the file is collected by default but the actual
benchmark step only runs under ``pytest --benchmark-only`` per the
project convention (see ``.github/workflows/ci.yml`` for the existing
``benchmarks/`` invocation).  Running this file with the default
``pytest`` invocation will NOT execute the benchmarks because each test
is also marked ``@pytest.mark.benchmark``; the project's CI fast lane
deselects this directory entirely.

Networked or external-LLM-dependent benchmarks are explicitly out of
scope for Phase 1 — see the related PR description.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# pytest-benchmark is a project dev-dependency declared in pyproject.toml,
# but skip cleanly if a downstream packager strips dev extras.
pytest.importorskip("pytest_benchmark")


# ── /api/status p50 latency ───────────────────────────────────────────────


@pytest.fixture
def status_client(tmp_path: Path, register_recording_backend: None) -> Iterator[Any]:
    """Boot a real Daemon + TestClient solely for the /api/status bench.

    Kept narrow so the fixture's setup cost is amortized across the
    benchmark rounds rather than bleeding into the measurements.
    """
    import asyncio

    from fastapi.testclient import TestClient

    from maxwell_daemon.api import create_app
    from maxwell_daemon.config import MaxwellDaemonConfig
    from maxwell_daemon.daemon import Daemon

    cfg = MaxwellDaemonConfig.model_validate(
        {
            "backends": {"primary": {"type": "recording", "model": "perf"}},
            "agent": {"default_backend": "primary"},
            "budget": {"monthly_limit_usd": 100.0, "hard_stop": False},
        }
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    daemon = Daemon(
        cfg,
        ledger_path=tmp_path / "ledger.db",
        task_store_path=tmp_path / "tasks.db",
    )
    loop.run_until_complete(daemon.start(worker_count=1))
    with TestClient(create_app(daemon)) as client:
        try:
            yield client
        finally:
            loop.run_until_complete(daemon.stop())
            loop.close()
            asyncio.set_event_loop(None)


@pytest.mark.benchmark
def test_api_status_p50(status_client: Any, benchmark: Any) -> None:
    """Bench ``GET /api/status``; pytest-benchmark reports p50/min/median."""

    def _hit() -> None:
        r = status_client.get("/api/status")
        assert r.status_code == 200

    benchmark(_hit)


# ── DispatchRequest validation throughput ─────────────────────────────────


@pytest.mark.benchmark
def test_dispatch_envelope_validation_throughput(benchmark: Any) -> None:
    """Bench Pydantic shape validation for the dispatch envelope."""
    from maxwell_daemon.api.contract import DispatchRequest

    payload = {
        "confirmation_token": "perf-token",
        "prompt": "perf bench prompt",
        "repo": "user/example",
        "idempotency_key": "perf-1",
    }

    def _validate() -> DispatchRequest:
        return DispatchRequest.model_validate(payload)

    result = benchmark(_validate)
    assert result.idempotency_key == "perf-1"


# ── TaskStore save() throughput ───────────────────────────────────────────


@pytest.mark.benchmark
def test_task_store_save_throughput(benchmark: Any, tmp_path: Path) -> None:
    """Bench ``TaskStore.save`` — the SQLite write path on every enqueue.

    Mirrors the existing ``tests/benchmark/test_task_store_benchmark.py``
    style (the brief asks specifically for ``add()`` throughput, but the
    real public name on this class is ``save``; the new repo-wide naming
    is settled and ``add`` no longer exists, so we bench what's actually
    on the hot path).
    """
    from maxwell_daemon.core.task_store import TaskStore
    from maxwell_daemon.daemon.runner import Task, TaskStatus

    store = TaskStore(tmp_path / "perf-tasks.db")
    counter = {"i": 0}

    def _save() -> None:
        counter["i"] += 1
        store.save(
            Task(
                id=f"perf-{counter['i']}",
                prompt="perf",
                status=TaskStatus.QUEUED,
                created_at=datetime.now(timezone.utc),
            )
        )

    benchmark(_save)
