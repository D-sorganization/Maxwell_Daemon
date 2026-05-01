"""Fleet coordinator logic extracted from runner.py (phase 2 of #798).

Handles the coordinator role: probing remote workers, dispatching queued tasks
to healthy machines via :class:`FleetDispatcher`, and requeueing tasks whose
assigned machine has gone offline.

This module is imported by :class:`~maxwell_daemon.daemon.runner.Daemon` when
the daemon starts in ``coordinator`` role.  No business logic lives here —
only fleet topology management and remote dispatch.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from maxwell_daemon.logging import get_logger

if TYPE_CHECKING:
    from maxwell_daemon.config import MaxwellDaemonConfig
    from maxwell_daemon.core.task_store import TaskStore
    from maxwell_daemon.daemon.task_models import Task

log = get_logger("maxwell_daemon.daemon.fleet_coordinator")

__all__ = ["FleetCoordinator"]


class FleetCoordinator:
    """Encapsulates coordinator-role fleet dispatch logic.

    A :class:`~maxwell_daemon.daemon.runner.Daemon` running as ``coordinator``
    creates one :class:`FleetCoordinator` and drives it via
    :meth:`run_loop`.  The coordinator never executes tasks locally; it only
    dispatches them to remote workers and monitors liveness.

    Parameters
    ----------
    config:
        Active daemon configuration (may be hot-reloaded via :meth:`update_config`).
    tasks:
        Shared in-memory task dict (same object as ``Daemon._tasks``).
    tasks_lock:
        Threading lock guarding iteration of *tasks*.
    task_store:
        Durable task store used to persist status changes.
    worker_last_seen:
        Map from machine name to last heartbeat timestamp (shared with daemon).
    enqueue_task_entry:
        Callable matching ``Daemon._enqueue_task_entry`` — used to requeue
        stale dispatched tasks back into the local priority queue.
    running_flag:
        Callable returning ``bool`` — should return ``True`` while the daemon
        is alive.  Typically a lambda closing over ``Daemon._running``.
    """

    def __init__(
        self,
        *,
        config: MaxwellDaemonConfig,
        tasks: dict[str, Task],
        tasks_lock: threading.Lock,
        task_store: TaskStore,
        worker_last_seen: dict[str, datetime],
        enqueue_task_entry: Any,
        running_flag: Any,
    ) -> None:
        self._config = config
        self._tasks = tasks
        self._tasks_lock = tasks_lock
        self._task_store = task_store
        self._worker_last_seen = worker_last_seen
        self._enqueue_task_entry = enqueue_task_entry
        self._running_flag = running_flag

    def update_config(self, config: MaxwellDaemonConfig) -> None:
        """Swap the active config (used during hot-reload)."""
        self._config = config

    async def run_loop(self) -> None:
        """Coordinator event loop — runs until the daemon stops."""
        poll_seconds = self._config.fleet_coordinator_poll_seconds
        while self._running_flag():
            try:
                await self._dispatch_tick()
            except Exception:
                log.exception("coordinator dispatch error")
            await asyncio.sleep(poll_seconds)

    async def _dispatch_tick(self) -> None:  # noqa: C901
        """One coordinator dispatch tick: probe machines, plan, submit, requeue stale tasks."""
        from maxwell_daemon.daemon.task_models import TaskStatus
        from maxwell_daemon.fleet.client import RemoteDaemonClient, RemoteDaemonError
        from maxwell_daemon.fleet.dispatcher import (
            FleetDispatcher,
            MachineState,
            TaskRequirement,
        )

        fleet_machines = self._config.fleet_machines
        if not fleet_machines:
            return

        # Build initial MachineState snapshots from config.
        initial_machines = tuple(
            MachineState(
                name=m.name,
                host=m.host,
                port=m.port,
                capacity=m.capacity,
                tags=tuple(m.tags),
            )
            for m in fleet_machines
        )

        client = RemoteDaemonClient(
            auth_token=self._config.api_auth_token,
        )

        # Probe all machines in parallel to get live health.
        machines = await client.refresh_all(initial_machines)

        # Requeue tasks dispatched to machines that have gone offline.
        now = datetime.now(timezone.utc)
        stale_threshold = self._config.fleet_heartbeat_seconds * 3
        with self._tasks_lock:
            tasks_snapshot = dict(self._tasks)

        for t in tasks_snapshot.values():
            if t.status is not TaskStatus.DISPATCHED or t.dispatched_to is None:
                continue
            machine_name = t.dispatched_to
            machine_healthy = any(m.name == machine_name and m.healthy for m in machines)
            if not machine_healthy:
                last_seen = self._worker_last_seen.get(machine_name)
                stale = True
                if last_seen is not None:
                    elapsed = (now - last_seen).total_seconds()
                    stale = elapsed > stale_threshold
                if stale:
                    self._handle_stale_dispatched_task(t, machine_name)

        # Collect tasks still QUEUED after potential requeuing above.
        with self._tasks_lock:
            tasks_snapshot = dict(self._tasks)

        queued_tasks = [t for t in tasks_snapshot.values() if t.status is TaskStatus.QUEUED]
        if not queued_tasks:
            return

        task_requirements = tuple(TaskRequirement(task_id=t.id) for t in queued_tasks)

        # Tally active_tasks on each machine from known DISPATCHED tasks.
        dispatched_counts: dict[str, int] = {}
        for t in tasks_snapshot.values():
            if t.status is TaskStatus.DISPATCHED and t.dispatched_to:
                dispatched_counts[t.dispatched_to] = dispatched_counts.get(t.dispatched_to, 0) + 1

        machines_with_load = tuple(
            MachineState(
                name=m.name,
                host=m.host,
                port=m.port,
                capacity=m.capacity,
                tags=m.tags,
                active_tasks=dispatched_counts.get(m.name, 0),
                healthy=m.healthy,
            )
            for m in machines
        )

        dispatcher = FleetDispatcher()
        plan = dispatcher.plan(machines_with_load, task_requirements)

        # Build lookup maps for fast resolution.
        tasks_by_id = {t.id: t for t in queued_tasks}
        machines_by_name = {m.name: m for m in machines}

        for assignment in plan.assignments:
            assigned_task = tasks_by_id.get(assignment.task_id)
            machine = machines_by_name.get(assignment.machine_name)
            if assigned_task is None or machine is None:
                continue

            task_payload: dict[str, Any] = {
                "task_id": assigned_task.id,
                "prompt": assigned_task.prompt,
                "kind": assigned_task.kind.value,
                "repo": assigned_task.repo,
                "backend": assigned_task.backend,
                "model": assigned_task.model,
                "issue_repo": assigned_task.issue_repo,
                "issue_number": assigned_task.issue_number,
                "issue_mode": assigned_task.issue_mode,
                "priority": assigned_task.priority,
            }

            try:
                result = await client.submit_task(machine, task_payload=task_payload)
            except RemoteDaemonError:
                log.exception(
                    "failed to dispatch task %s to machine %s",
                    assigned_task.id,
                    machine.name,
                )
                continue

            if result.status == "submitted":
                assigned_task.status = TaskStatus.DISPATCHED
                assigned_task.dispatched_to = machine.name
                log.info("dispatched task %s to machine %s", assigned_task.id, machine.name)
                try:
                    self._task_store.save(assigned_task)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "Failed to persist DISPATCHED state for task %s: %s",
                        assigned_task.id,
                        exc,
                        exc_info=True,
                    )
            else:
                log.warning(
                    "machine %s rejected task %s: %s",
                    machine.name,
                    assigned_task.id,
                    result.detail,
                )

        if plan.unassigned:
            log.debug(
                "coordinator: %d task(s) could not be placed this tick: %s",
                len(plan.unassigned),
                plan.unassigned,
            )

    def _handle_stale_dispatched_task(self, task: Task, machine_name: str) -> None:
        """Fail or requeue a dispatched task whose worker machine is no longer healthy."""
        if task.side_effects_started:
            log.warning(
                "worker %s appears offline after task %s started side effects; "
                "failing instead of transparent failover",
                machine_name,
                task.id,
            )
            from maxwell_daemon.daemon.task_models import TaskStatus

            task.status = TaskStatus.FAILED
            task.error = (
                f"worker {machine_name} became stale after side effects started; "
                "retry as a new attempt"
            )
            task.finished_at = datetime.now(timezone.utc)
            self._task_store.save(task)
            return

        log.warning(
            "worker %s appears offline before task %s started side effects; requeueing",
            machine_name,
            task.id,
        )
        from maxwell_daemon.daemon.task_models import TaskStatus

        task.status = TaskStatus.QUEUED
        task.dispatched_to = None
        self._task_store.save(task)
        self._enqueue_task_entry(task.priority, task)
