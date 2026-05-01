"""Unit tests for FleetCoordinator (issue #798, phase 2).

Tests focus on the stale-task handling logic which is pure synchronous code
and can be exercised without running a real fleet.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from maxwell_daemon.daemon.fleet_coordinator import FleetCoordinator
from maxwell_daemon.daemon.task_models import Task, TaskKind, TaskStatus


def _make_task(
    *,
    status: TaskStatus = TaskStatus.DISPATCHED,
    dispatched_to: str | None = "worker-1",
    side_effects_started: bool = False,
) -> Task:
    """Build a minimal Task for fleet coordinator tests."""
    return Task(
        id="test-task-abc",
        prompt="test",
        kind=TaskKind.PROMPT,
        status=status,
        dispatched_to=dispatched_to,
        side_effects_started=side_effects_started,
    )


def _make_coordinator(
    *,
    tasks: dict | None = None,
    task_store: MagicMock | None = None,
    enqueue_fn: MagicMock | None = None,
) -> FleetCoordinator:
    """Build a FleetCoordinator with minimal mock dependencies."""
    config = SimpleNamespace(
        fleet_machines=[],
        fleet_coordinator_poll_seconds=5,
        fleet_heartbeat_seconds=30,
        api_auth_token=None,
    )
    return FleetCoordinator(
        config=config,
        tasks=tasks or {},
        tasks_lock=threading.Lock(),
        task_store=task_store or MagicMock(),
        worker_last_seen={},
        enqueue_task_entry=enqueue_fn or MagicMock(),
        running_flag=lambda: False,
    )


class TestHandleStaleDispatchedTask:
    def test_requeueing_when_no_side_effects_started(self) -> None:
        """Tasks with no side effects should be requeued transparently."""
        enqueue_fn = MagicMock()
        task_store = MagicMock()
        coordinator = _make_coordinator(enqueue_fn=enqueue_fn, task_store=task_store)
        task = _make_task(side_effects_started=False)

        coordinator._handle_stale_dispatched_task(task, "worker-1")

        assert task.status is TaskStatus.QUEUED
        assert task.dispatched_to is None
        task_store.save.assert_called_once_with(task)
        enqueue_fn.assert_called_once_with(task.priority, task)

    def test_failing_when_side_effects_started(self) -> None:
        """Tasks that started side effects must not be transparently failed."""
        enqueue_fn = MagicMock()
        task_store = MagicMock()
        coordinator = _make_coordinator(enqueue_fn=enqueue_fn, task_store=task_store)
        task = _make_task(side_effects_started=True)

        coordinator._handle_stale_dispatched_task(task, "worker-1")

        assert task.status is TaskStatus.FAILED
        assert task.error is not None
        assert "side effects" in task.error
        task_store.save.assert_called_once_with(task)
        # Must NOT re-enqueue when side effects already started
        enqueue_fn.assert_not_called()

    def test_failed_task_has_finished_at_set(self) -> None:
        """A failed stale task should record a finish timestamp."""
        coordinator = _make_coordinator()
        task = _make_task(side_effects_started=True)

        coordinator._handle_stale_dispatched_task(task, "worker-1")

        assert task.finished_at is not None
        # finished_at should be close to now
        age = datetime.now(timezone.utc) - task.finished_at
        assert age < timedelta(seconds=5)


class TestUpdateConfig:
    def test_config_is_swapped(self) -> None:
        """update_config should replace the internal config reference."""
        coordinator = _make_coordinator()
        new_config = SimpleNamespace(
            fleet_machines=[],
            fleet_coordinator_poll_seconds=10,
            fleet_heartbeat_seconds=60,
            api_auth_token="token",
        )
        coordinator.update_config(new_config)
        assert coordinator._config is new_config
