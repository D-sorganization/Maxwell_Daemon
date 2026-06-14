"""Extracted daemon behavior mixin for runner.py shrinkage (#987)."""

from __future__ import annotations

# mypy: disable-error-code=attr-defined
import asyncio
import concurrent.futures
import uuid
from datetime import datetime, timezone
from typing import Any

from maxwell_daemon.daemon.retry_policy import DEFAULT_RETRY_POLICY
from maxwell_daemon.daemon.task_models import (
    QueueSaturationError,
    Task,
    TaskKind,
)
from maxwell_daemon.events import Event, EventKind, attach_observability
from maxwell_daemon.logging import get_logger

log = get_logger("maxwell_daemon.daemon")


class DaemonSubmissionMixin:
    """Mixin extracted from daemon.runner."""

    def _enqueue_task_entry(self, priority: int, task: Task) -> None:
        """Insert a queue entry while respecting daemon loop thread affinity."""
        item: tuple[int, Task | object] = (priority, task)
        if self._loop is None or not self._loop.is_running():
            with self._queue_lock:
                if self._queue.full():
                    log.warning(
                        "queue is saturated (max_depth=%d)", self._config.agent.max_queue_depth
                    )
                    raise QueueSaturationError(
                        "Task queue is full, please try again later",
                        backoff_seconds=DEFAULT_RETRY_POLICY.queue_saturation_backoff(),
                    )
                try:
                    self._queue.put_nowait(item)
                except asyncio.QueueFull as exc:
                    raise QueueSaturationError(
                        "Task queue is full, please try again later",
                        backoff_seconds=DEFAULT_RETRY_POLICY.queue_saturation_backoff(),
                    ) from exc
            return
        loop = self._loop
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            # If we are on the event loop thread, we might be inside a signal handler.
            # Mutating the PriorityQueue inline can corrupt the heap if the signal
            # interrupted a heapq operation. Use call_soon_threadsafe to defer safely.
            def _put_inline() -> None:
                try:
                    if self._queue.full():
                        # The HTTP 200 has already been returned by the time this
                        # deferred callback runs, so we cannot raise 429 here.
                        # Instead of silently dropping the task (leaving it
                        # stranded QUEUED forever), transition it to FAILED and
                        # emit an event so the saturation is observable (#972).
                        self._fail_saturated_task(task)
                        return
                    self._queue.put_nowait(item)
                except asyncio.QueueFull:
                    self._fail_saturated_task(task)

            loop.call_soon_threadsafe(_put_inline)
            return

        result: concurrent.futures.Future[None] = concurrent.futures.Future()

        def _put() -> None:
            try:
                if self._queue.full():
                    result.set_exception(
                        QueueSaturationError(
                            "Task queue is full, please try again later",
                            backoff_seconds=DEFAULT_RETRY_POLICY.queue_saturation_backoff(),
                        )
                    )
                    return
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                log.warning("queue is saturated (max_depth=%d)", self._config.agent.max_queue_depth)
                result.set_exception(
                    QueueSaturationError(
                        "Task queue is full, please try again later",
                        backoff_seconds=DEFAULT_RETRY_POLICY.queue_saturation_backoff(),
                    )
                )
            except BaseException as exc:  # pragma: no cover - surfaced via Future  # noqa: BLE001
                result.set_exception(exc)
            else:
                result.set_result(None)

        loop.call_soon_threadsafe(_put)
        result.result(timeout=60.0)

    def submit(
        self,
        prompt: str,
        *,
        repo: str | None = None,
        backend: str | None = None,
        model: str | None = None,
        priority: int = 100,
        task_id: str | None = None,
        depends_on: list[str] | None = None,
        dry_run: bool = False,
    ) -> Task:
        resolved_task_id = task_id or uuid.uuid4().hex[:12]
        task = Task(
            id=resolved_task_id,
            prompt=self._offload_prompt_if_needed(resolved_task_id, prompt),
            kind=TaskKind.PROMPT,
            repo=repo,
            backend=backend,
            model=model,
            priority=priority,
            depends_on=list(depends_on) if depends_on else [],
            dry_run=dry_run,
        )
        # Persist and track the task under lock, then perform the queue
        # mutation after releasing it. The cross-thread enqueue path waits for
        # the daemon loop to run a callback, so holding _tasks_lock here can
        # deadlock if the loop is concurrently inside a maintenance path that
        # also needs the lock.
        with self._tasks_lock:
            if task_id is not None:
                self._reject_duplicate_task_id(task.id)
            self._task_store.save(task)
            self._tasks[task.id] = task
        try:
            self._enqueue_task_entry(task.priority, task)
        except QueueSaturationError:
            with self._tasks_lock:
                del self._tasks[task.id]
            self._task_store.delete(task.id)
            raise
        # Fire-and-forget: if there's no running loop yet (e.g. sync test
        # submits before start()), skip the event — the queued state is
        # observable via get_task().
        try:
            loop = asyncio.get_running_loop()
            # Task kept alive via strong reference in _bg_tasks.
            bg = loop.create_task(
                self._events.publish(
                    Event(
                        kind=EventKind.TASK_QUEUED,
                        payload=attach_observability(
                            {"id": task.id},
                            task_id=task.id,
                        ),
                    )
                )
            )
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            # No running event loop — called from a sync context before start().
            # The task is already enqueued; the missing event is acceptable here.
            pass
        return task

    def submit_threadsafe(
        self,
        prompt: str,
        *,
        repo: str | None = None,
        backend: str | None = None,
        model: str | None = None,
        dry_run: bool = False,
    ) -> Task:
        """Enqueue a prompt task from any thread. **Cross-thread safe.**

        Unlike :meth:`submit`, this method is safe to call from threads that
        are *not* running the daemon's event loop (e.g. WSGI middleware,
        background threads, sync test clients).  It uses
        ``asyncio.run_coroutine_threadsafe`` to schedule the queue put on the
        running event loop so the sleeping worker is reliably woken.

        :raises RuntimeError: if the daemon has not been started yet
            (``self._loop`` is ``None``).
        """
        if self._loop is None:
            raise RuntimeError("daemon must be started before submit_threadsafe()")

        loop = self._loop
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            return self.submit(
                prompt,
                repo=repo,
                backend=backend,
                model=model,
                dry_run=dry_run,
            )

        result: concurrent.futures.Future[Task] = concurrent.futures.Future()

        def _submit_on_loop() -> None:
            try:
                task = self.submit(
                    prompt,
                    repo=repo,
                    backend=backend,
                    model=model,
                    dry_run=dry_run,
                )
            except BaseException as exc:  # pragma: no cover - surfaced via Future  # noqa: BLE001
                result.set_exception(exc)
            else:
                result.set_result(task)

        self._loop.call_soon_threadsafe(_submit_on_loop)
        return result.result(timeout=60.0)

    def _offload_prompt_if_needed(self, task_id: str, prompt: str) -> str:
        """Move large prompts (>50KB) to the artifact store to keep task history lean.

        Returns the original prompt if small, or a truncated version with an
        artifact_id reference if large.
        """
        max_prompt_len = 50000
        offload_cutoff = 10000
        if len(prompt) <= max_prompt_len:
            return prompt

        from maxwell_daemon.core.artifacts import ArtifactKind

        # Migrate remainder to artifact store
        main_req = prompt[:offload_cutoff]
        remainder = prompt[offload_cutoff:]
        artifact = self._artifact_store.put_text(
            kind=ArtifactKind.METADATA,
            name="prompt_overflow.txt",
            text=remainder,
            task_id=task_id,
        )
        log.info("prompt-offload task=%s size=%d", task_id, len(prompt))
        return f"{main_req}\n\n[Full prompt offloaded to artifact_id:///{artifact.id}]"

    def submit_issue(
        self,
        *,
        repo: str,
        issue_number: int,
        mode: str = "plan",
        backend: str | None = None,
        model: str | None = None,
        priority: int = 100,
        task_id: str | None = None,
        dry_run: bool = False,
    ) -> Task:
        """Queue a task that reads a GitHub issue and opens a draft PR for it."""
        if mode not in {"plan", "implement"}:
            raise ValueError(f"mode must be 'plan' or 'implement', got {mode!r}")
        resolved_task_id = task_id or uuid.uuid4().hex[:12]
        task = Task(
            id=resolved_task_id,
            prompt=f"{repo}#{issue_number}",
            kind=TaskKind.ISSUE,
            repo=repo,
            backend=backend,
            model=model,
            issue_repo=repo,
            issue_number=issue_number,
            issue_mode=mode,
            priority=priority,
            dry_run=dry_run,
        )
        # See note in submit(): the queue mutation waits for the daemon loop to
        # run a callback, so it must happen *outside* _tasks_lock — holding the
        # lock across the cross-thread enqueue can deadlock the loop. Persist and
        # track under the lock, enqueue after release, and roll back on
        # saturation so a rejected task leaves no orphaned store/registry row.
        with self._tasks_lock:
            if task_id is not None:
                self._reject_duplicate_task_id(task.id)
            self._task_store.save(task)
            self._tasks[task.id] = task
        try:
            self._enqueue_task_entry(task.priority, task)
        except QueueSaturationError:
            with self._tasks_lock:
                del self._tasks[task.id]
            self._task_store.delete(task.id)
            raise
        try:
            loop = asyncio.get_running_loop()
            bg = loop.create_task(
                self._events.publish(
                    Event(
                        kind=EventKind.TASK_QUEUED,
                        payload={
                            "id": task.id,
                            "kind": "issue",
                            "repo": repo,
                            "issue": issue_number,
                        },
                    )
                )
            )
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            # No running event loop — called from a sync context before start().
            # The task is already enqueued; the missing event is acceptable here.
            pass
        return task

    def submit_issue_ab(
        self,
        *,
        repo: str,
        issue_number: int,
        backends: list[str],
        mode: str = "plan",
        dry_run: bool = False,
    ) -> list[Task]:
        """Dispatch the same issue to multiple backends concurrently.

        Tasks share an ``ab_group`` so the UI can pair them and a reviewer can
        compare PRs side-by-side.
        """
        if len(backends) < 2:
            raise ValueError("A/B dispatch needs at least two backends")
        if len(set(backends)) != len(backends):
            raise ValueError("A/B dispatch backends must be distinct")
        ab_group = uuid.uuid4().hex[:12]
        tasks: list[Task] = []
        for backend in backends:
            # Let submit_issue do the regular queueing, then tag the group.
            task = self.submit_issue(
                repo=repo,
                issue_number=issue_number,
                mode=mode,
                backend=backend,
                dry_run=dry_run,
            )
            task.ab_group = ab_group
            # Persist the group so recovery sees it too.
            self._task_store.save(task)
            tasks.append(task)
        return tasks

    def set_issue_collaborators(
        self,
        *,
        github_client: Any,
        workspace: Any,
        executor_factory: Any,
    ) -> None:
        """Inject issue-dispatch collaborators (used by tests + server setup)."""
        self._github_client = github_client
        self._workspace = workspace
        self._issue_executor_factory = executor_factory

    def record_worker_heartbeat(self, machine_name: str) -> None:
        """Update last-seen timestamp for a worker machine (called by heartbeat endpoint)."""
        self._worker_last_seen[machine_name] = datetime.now(timezone.utc)
