"""Benchmarks for task store operations."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from maxwell_daemon.core.task_store import TaskStore


@pytest.fixture
def task_store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.db")


def test_save_task(benchmark: pytest.BenchmarkFixture, task_store: TaskStore) -> None:
    """Benchmark task save operation."""
    from maxwell_daemon.daemon.runner import Task, TaskStatus

    task = Task(
        id="task-1",
        prompt="benchmark prompt",
        status=TaskStatus.QUEUED,
        created_at=datetime.now(timezone.utc),
    )

    def _save() -> None:
        task_store.save(task)

    benchmark(_save)


def test_get_task(benchmark: pytest.BenchmarkFixture, task_store: TaskStore) -> None:
    """Benchmark task retrieval by ID."""
    from maxwell_daemon.daemon.runner import Task, TaskStatus

    task = Task(
        id="task-get",
        prompt="benchmark prompt",
        status=TaskStatus.QUEUED,
        created_at=datetime.now(timezone.utc),
    )
    task_store.save(task)
    benchmark(task_store.get, "task-get")


def test_list_tasks(benchmark: pytest.BenchmarkFixture, task_store: TaskStore) -> None:
    """Benchmark paginated task listing."""
    from maxwell_daemon.daemon.runner import Task, TaskStatus

    for i in range(1000):
        task = Task(
            id=f"task-{i}",
            prompt=f"prompt {i}",
            status=TaskStatus.QUEUED,
            created_at=datetime.now(timezone.utc),
        )
        task_store.save(task)

    benchmark(task_store.list_tasks, limit=100)
