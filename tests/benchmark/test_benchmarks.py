"""Phase-1 benchmark scaffolding for issue #800.

A single, self-contained ``pytest-benchmark`` test that times task list
retrieval from the persistent ``TaskStore``.  Guarded by ``importorskip`` so
the suite degrades cleanly when the plugin is unavailable (CLAUDE.md §3 —
optional dependency hygiene).

Follow-up phases will extend this file with API-roundtrip benchmarks,
cost-ledger queries, and regression baselines.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("pytest_benchmark")

from maxwell_daemon.core.task_store import TaskStore
from maxwell_daemon.daemon.runner import Task, TaskStatus


@pytest.fixture
def populated_task_store(tmp_path: Path) -> TaskStore:
    """A task store pre-loaded with 500 tasks for a representative list query."""
    store = TaskStore(tmp_path / "tasks.db")
    now = datetime.now(timezone.utc)
    for i in range(500):
        store.save(
            Task(
                id=f"benchmark-task-{i:04d}",
                prompt=f"benchmark prompt {i}",
                status=TaskStatus.QUEUED,
                created_at=now,
            )
        )
    return store


def test_task_list_retrieval(
    benchmark: pytest.BenchmarkFixture, populated_task_store: TaskStore
) -> None:
    """Time a paginated ``list_tasks`` call against a 500-row store.

    This is the foundation benchmark for issue #800 — future phases will pin
    a budget (e.g. p95 < 50 ms) once we have baseline numbers across CI
    environments.  For now we just want a deterministic, repeatable signal.
    """
    result = benchmark(populated_task_store.list_tasks, limit=100)
    # Sanity check the benchmark actually exercised the code path.
    assert len(result) == 100
