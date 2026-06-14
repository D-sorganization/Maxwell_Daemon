"""Extracted daemon behavior mixin for runner.py shrinkage (#987)."""

from __future__ import annotations

# mypy: disable-error-code=attr-defined
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from maxwell_daemon.daemon.task_models import QueueSaturationError, Task, TaskStatus
from maxwell_daemon.events import Event, EventKind, attach_observability
from maxwell_daemon.logging import get_logger

log = get_logger("maxwell_daemon.daemon")


def _retry_policy() -> Any:
    """Return the runner-level retry policy override for compatibility."""
    from maxwell_daemon.daemon import runner as runner_mod

    return runner_mod.DEFAULT_RETRY_POLICY


class DaemonMaintenanceMixin:
    """Mixin extracted from daemon.runner."""

    def prune_retained_history(self, older_than_days: int | None = None) -> dict[str, int]:
        """Prune terminal tasks and ledger rows older than the retention window."""
        days = (
            self._config.agent.task_retention_days if older_than_days is None else older_than_days
        )
        if days <= 0:
            return {"tasks": 0, "ledger_records": 0}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        with self._tasks_lock:
            stale_ids = [
                task_id
                for task_id, task in self._tasks.items()
                if task.status in terminal
                and task.finished_at is not None
                and task.finished_at < cutoff
            ]
            for task_id in stale_ids:
                self._tasks.pop(task_id, None)

        pruned_tasks = self._task_store.prune(days)
        pruned_ledger = self._ledger.prune(days)
        return {"tasks": pruned_tasks, "ledger_records": pruned_ledger}

    async def aprune_retained_history(self, older_than_days: int | None = None) -> dict[str, int]:
        """Prune retained history without blocking the event loop on SQLite work."""
        days = (
            self._config.agent.task_retention_days if older_than_days is None else older_than_days
        )
        if days <= 0:
            return {"tasks": 0, "ledger_records": 0}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        with self._tasks_lock:
            stale_ids = [
                task_id
                for task_id, task in self._tasks.items()
                if task.status in terminal
                and task.finished_at is not None
                and task.finished_at < cutoff
            ]
            for task_id in stale_ids:
                self._tasks.pop(task_id, None)

        pruned_tasks, pruned_ledger = await asyncio.gather(
            self._task_store.aprune(days),
            self._ledger.aprune(days),
        )
        return {"tasks": pruned_tasks, "ledger_records": pruned_ledger}

    async def _retention_loop(self) -> None:
        interval = self._config.agent.task_prune_interval_seconds
        while self._running:
            try:
                result = await self.aprune_retained_history()
                if result["tasks"] or result["ledger_records"]:
                    log.info("retention prune completed: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("retention prune failed", exc_info=True)
            await asyncio.sleep(interval)

    async def _live_eviction_loop(self) -> None:
        """Periodically evict terminal tasks from the live memory dict."""
        while self._running:
            try:
                live_retention = self._config.agent.task_live_retention_seconds
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=live_retention)
                terminal = {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }
                evicted = 0
                with self._tasks_lock:
                    stale_ids = [
                        task_id
                        for task_id, task in self._tasks.items()
                        if task.status in terminal
                        and task.finished_at is not None
                        and task.finished_at < cutoff
                    ]
                    for task_id in stale_ids:
                        self._tasks.pop(task_id, None)
                        evicted += 1
                if evicted > 0:
                    log.debug("evicted %d stale tasks from live memory dict", evicted)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("live eviction loop failed", exc_info=True)
            await asyncio.sleep(60.0)

    async def _stall_reconcile_loop(self) -> None:
        """Periodically cancel and retry RUNNING tasks that go silent."""
        while self._running:
            try:
                await self._reconcile_stalled_runs()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("stall reconcile loop failed", exc_info=True)
            timeout = self._config.agent.stall_timeout_seconds
            interval = 1.0 if timeout <= 2 else min(timeout / 2.0, 30.0)
            await asyncio.sleep(interval)

    async def _reconcile_stalled_runs(self) -> int:
        """Cancel RUNNING tasks that have exceeded the configured silence window."""
        timeout = self._config.agent.stall_timeout_seconds
        if timeout <= 0:
            return 0
        now = datetime.now(timezone.utc)
        stalled: list[tuple[Task, asyncio.Task[None], float, str | None]] = []
        with self._tasks_lock:
            for task_id, handle in self._active_execution_tasks.items():
                if handle.done():
                    continue
                task = self._tasks.get(task_id)
                if task is None or task.status is not TaskStatus.RUNNING:
                    continue
                anchor = self._last_stream_event_at.get(task_id) or task.started_at
                if anchor is None:
                    continue
                elapsed_seconds = (now - anchor).total_seconds()
                if elapsed_seconds <= timeout:
                    continue
                stalled.append(
                    (
                        task,
                        handle,
                        elapsed_seconds,
                        self._last_stream_event_kind.get(task_id),
                    )
                )
                self._stalled_task_ids.add(task_id)
        for task, handle, elapsed_seconds, last_event_kind in stalled:
            log.warning(
                "stall_detected",
                task_id=task.id,
                elapsed_seconds=round(elapsed_seconds, 3),
                last_event_kind=last_event_kind,
            )
            handle.cancel()
        return len(stalled)

    def _record_stream_event(self, task_id: str, kind: str) -> None:
        self._last_stream_event_at[task_id] = datetime.now(timezone.utc)
        self._last_stream_event_kind[task_id] = kind

    def _clear_execution_tracking(self, task_id: str) -> None:
        self._active_execution_tasks.pop(task_id, None)
        self._last_stream_event_at.pop(task_id, None)
        self._last_stream_event_kind.pop(task_id, None)

    async def _handle_stalled_task(self, task: Task) -> None:
        message = (
            "Task exceeded agent.stall_timeout_seconds without progress; "
            "cancelling and re-queueing."
        )
        task.status = TaskStatus.FAILED
        task.error = message
        task.finished_at = datetime.now(timezone.utc)
        try:
            self._task_store.save(task)
        except Exception:
            log.exception("task store write failed while recording stalled task=%s", task.id)
        await self._events.publish(
            Event(
                kind=EventKind.TASK_FAILED,
                payload=attach_observability(
                    {"id": task.id, "error": message, "reason": "stalled"},
                    task_id=task.id,
                    backend=task.backend,
                    model=task.model,
                ),
            )
        )

        # A task that already started irreversible side effects (opened a PR,
        # posted a comment) must NOT be auto-retried — a re-run would duplicate
        # them. Leave it permanently FAILED with a clear reason (#971).
        if task.side_effects_started:
            log.warning("stalled task %s had side_effects_started; not auto-retrying", task.id)
            return

        # Gate the stall retry on the RetryPolicy so a perpetually-stalling task
        # fails permanently after max_retries instead of looping forever with
        # zero backoff and unbounded spend (#971).
        if not _retry_policy().should_retry(task):
            log.warning(
                "stalled task %s exhausted retry budget (retry_count=%d); failing permanently",
                task.id,
                task.retry_count,
            )
            return

        delay = _retry_policy().next_retry_delay(task.retry_count)
        retried = self.retry_task(task.id, expected_status=TaskStatus.FAILED)
        retried.retry_count += 1
        try:
            self._task_store.save(retried)
        except Exception:
            log.exception("task store write failed while bumping retry_count task=%s", task.id)
        # Re-enqueue after the backoff delay rather than immediately, so retries
        # don't form a tight cancel/re-run loop.
        self._schedule_delayed_enqueue(retried, delay.total_seconds())

    def _schedule_delayed_enqueue(self, task: Task, delay_seconds: float) -> None:
        """Re-enqueue ``task`` after ``delay_seconds`` via a tracked bg task."""

        async def _delayed() -> None:
            try:
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)
                self._enqueue_task_entry(task.priority, task)
            except asyncio.CancelledError:
                raise
            except (QueueSaturationError, RuntimeError):
                log.warning("delayed re-enqueue failed for task=%s", task.id, exc_info=True)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. unit context): enqueue inline.
            self._enqueue_task_entry(task.priority, task)
            return
        bg = loop.create_task(_delayed())
        self._bg_tasks.add(bg)
        bg.add_done_callback(self._bg_tasks.discard)

    def _fail_saturated_task(self, task: Task | None) -> None:
        """Mark a task FAILED + emit an event when the on-loop enqueue is full.

        The on-loop submission path defers the actual queue ``put`` to a
        callback that runs after the HTTP response is already sent. If the queue
        is saturated at that point we cannot return 429, so we make the drop
        observable instead of leaving the persisted task stranded in QUEUED with
        no queue entry and no error (#972).
        """
        if task is None:
            return
        message = (
            f"Task queue saturated (max_depth={self._config.agent.max_queue_depth}); "
            "submission dropped before it could be enqueued."
        )
        log.warning("queue saturated; failing dropped task %s", task.id)
        task.status = TaskStatus.FAILED
        task.error = message
        task.finished_at = datetime.now(timezone.utc)
        try:
            self._task_store.save(task)
        except Exception:
            log.exception("task store write failed while failing saturated task=%s", task.id)
        bg = asyncio.ensure_future(
            self._events.publish(
                Event(
                    kind=EventKind.TASK_FAILED,
                    payload=attach_observability(
                        {"id": task.id, "error": message, "reason": "queue_saturated"},
                        task_id=task.id,
                        backend=task.backend,
                        model=task.model,
                    ),
                )
            )
        )
        self._bg_tasks.add(bg)
        bg.add_done_callback(self._bg_tasks.discard)

    async def _dream_cycle_loop(self) -> None:
        """Periodically consolidate raw markdown memory when explicitly enabled."""
        while self._running:
            interval = self._config.memory_dream_interval_seconds
            if interval <= 0:
                return
            await asyncio.sleep(interval)
            if not self._running:
                return
            try:
                result = await self.run_memory_dream_cycle()
                log.info("memory dream cycle completed: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("memory dream cycle failed", exc_info=True)

    async def run_memory_dream_cycle(self) -> str:
        """Run one memory anneal pass against the configured local markdown store."""
        from maxwell_daemon.core.memory_annealer import MemoryAnnealer
        from maxwell_daemon.core.roles import Role, RoleOrchestrator

        annealer = MemoryAnnealer(workspace=self._config.memory_workspace_path)
        if annealer.status().raw_log_count == 0:
            return "No raw memory to anneal."

        role = Role(
            name="memory_summarizer",
            system_prompt=(
                "You consolidate raw Maxwell-Daemon execution logs into concise, durable "
                "markdown memory. Preserve technical decisions, repository conventions, "
                "and lessons learned. Drop transient chatter and secrets."
            ),
        )
        summarizer = RoleOrchestrator(self._router).assign_player(role)
        return await MemoryAnnealer(
            workspace=self._config.memory_workspace_path,
            summarizer_role=summarizer,
        ).anneal()
