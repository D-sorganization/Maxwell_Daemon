"""Worker execution loop for the Maxwell daemon.

This module holds the task-claim, execution, and issue-execution paths that
used to live in daemon.runner. Daemon inherits this mixin so existing private
worker hooks remain available while runner.py keeps shrinking under #987.
"""

from __future__ import annotations

# mypy: disable-error-code=attr-defined
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from maxwell_daemon.backends import Message, MessageRole
from maxwell_daemon.core import BudgetExceededError, CostRecord
from maxwell_daemon.daemon.task_models import Task, TaskKind, TaskStatus
from maxwell_daemon.events import Event, EventKind, attach_observability
from maxwell_daemon.logging import get_logger
from maxwell_daemon.metrics import record_request
from maxwell_daemon.tracing import span as _trace_span

log = get_logger("maxwell_daemon.daemon")


class WorkerExecutionMixin:
    """Task worker and execution behavior for Daemon."""

    async def _worker_loop(self, worker_id: int) -> None:
        from maxwell_daemon.logging import bind_context

        log.info("worker %d ready", worker_id)
        while self._running or not self._queue.empty():
            should_exit, task = await self._claim_next_queued_task()
            if should_exit:
                log.info("worker %d received stop sentinel; exiting", worker_id)
                break
            if task is None:
                await asyncio.sleep(0.05)
                continue
            with bind_context(task_id=task.id, worker_id=worker_id):
                # Supervision boundary (#973): an unexpected exception escaping a
                # single task must NOT kill the worker coroutine — otherwise each
                # incident (disk-full, lock-timeout) permanently drops a worker
                # while ``state()`` still reports it alive.  CancelledError is the
                # one exception we re-raise: it is the loop's own shutdown signal.
                try:
                    await self._run_one_task(task)
                except asyncio.CancelledError:
                    raise
                except Exception:  # last-resort worker supervision
                    log.exception("worker %d: task %s crashed; continuing", worker_id, task.id)

    async def _run_one_task(self, task: Task) -> None:
        # Attempt to mark the task RUNNING in the durable store before
        # executing.  If that write fails (disk full, lock contention),
        # re-queue the task so it is retried rather than silently lost.
        # Double-execution is still possible if the DB write succeeds but
        # the worker crashes before execution completes; preventing that
        # fully requires a lease/heartbeat mechanism (future work).
        try:
            self._task_store.update_status(task.id, TaskStatus.RUNNING, started_at=task.started_at)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "failed to mark task %s RUNNING: %s; re-queuing",
                task.id,
                exc,
            )
            with self._tasks_lock:
                if task.status is TaskStatus.RUNNING:
                    task.status = TaskStatus.QUEUED
                    task.started_at = None
            await self._queue.put((task.priority, task))
            return
        snapshot = self._capture_config_snapshot()
        execution = asyncio.create_task(
            self._execute(task, snapshot),
            name=f"task-exec-{task.id}",
        )
        self._active_execution_tasks[task.id] = execution
        try:
            await execution
        except asyncio.CancelledError:
            if task.id in self._stalled_task_ids:
                self._stalled_task_ids.discard(task.id)
                await self._handle_stalled_task(task)
                return
            raise
        finally:
            self._clear_execution_tracking(task.id)

    async def _execute(self, task: Task, snapshot: Any) -> None:  # noqa: C901
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)
        try:
            self._task_store.update_status(task.id, TaskStatus.RUNNING, started_at=task.started_at)
        except Exception:
            log.exception("task store write failed for task=%s", task.id)
            raise
        decision_backend = "unknown"
        decision_model = "unknown"
        repo_path = None
        try:
            target_repo = task.repo or task.issue_repo
            if target_repo:
                repo_cfg = next((r for r in snapshot.config.repos if r.name == target_repo), None)
                if repo_cfg and repo_cfg.path:
                    repo_path = Path(repo_cfg.path)
                else:
                    from maxwell_daemon.gh.workspace import Workspace

                    ws = getattr(self, "_workspace", None) or Workspace(root=self._workspace_root)
                    repo_path = await ws.ensure_clone(target_repo, task_id=task.id)

                from maxwell_daemon.daemon.workspace_hooks import execute_hooks, load_hooks_config

                hook_config = load_hooks_config(repo_path, global_config=snapshot.config)
                if hook_config:
                    await execute_hooks("before_run", repo_path, config=hook_config, fatal=True)

            await self._events.publish(
                Event(
                    kind=EventKind.TASK_STARTED,
                    payload=attach_observability(
                        {"id": task.id, "prompt": task.prompt},
                        task_id=task.id,
                    ),
                )
            )
            self._record_stream_event(task.id, EventKind.TASK_STARTED.value)
            # Wrap the dispatch path (routing → backend completion) in a span so
            # a trace shows the task lifecycle end-to-end. The span is a no-op
            # when tracing is disabled (zero-cost default install). Exceptions
            # propagate *through* the span — it records them and sets an ERROR
            # status — and are then handled by the except clauses below.
            async with _trace_span(
                "maxwell_daemon.task.dispatch",
                {"task_id": task.id, "kind": task.kind.value},
            ):
                snapshot.budget.require_under_budget()
                decision = snapshot.router.route(
                    repo=task.repo,
                    backend_override=task.backend,
                    model_override=task.model,
                )
                task.backend = decision.backend_name
                task.route_reason = decision.reason
                if task.kind is not TaskKind.ISSUE:
                    task.model = decision.model
                decision_backend = task.backend or decision.backend_name
                decision_model = task.model or decision.model
                try:
                    self._task_store.save(task)
                except Exception:
                    log.exception(
                        "task store write failed while recording route for task=%s", task.id
                    )

                if task.kind is TaskKind.ISSUE:
                    await self._execute_issue(task, decision)
                    return

                prompt_content = task.prompt
                if task.repo and repo_path:
                    from maxwell_daemon.core.repo_overrides import RepoSchematic

                    schematic = RepoSchematic(task.repo, repo_path).generate()
                    prompt_content = f"{schematic}\n\n{prompt_content}"

                async with _trace_span(
                    "maxwell_daemon.task.backend_complete",
                    {
                        "task_id": task.id,
                        "backend": decision.backend_name,
                        "model": decision.model,
                    },
                ):
                    resp = await decision.backend.complete(
                        [Message(role=MessageRole.USER, content=prompt_content)],
                        model=decision.model,
                        dry_run=task.dry_run,
                    )
            task.result = resp.content
            estimated_cost = decision.backend.estimate_cost(resp.usage, decision.model)
            task.cost_usd = estimated_cost if estimated_cost is not None else 0.0
            task.status = TaskStatus.COMPLETED
            snapshot.ledger.record(
                CostRecord(
                    ts=datetime.now(timezone.utc),
                    backend=decision.backend_name,
                    model=decision.model,
                    usage=resp.usage,
                    cost_usd=task.cost_usd,
                    repo=task.repo,
                    agent_id=task.id,
                )
            )
            record_request(
                backend=decision.backend_name,
                model=decision.model,
                status="success",
                tokens=resp.usage.total_tokens,
                cost_usd=task.cost_usd,
                duration_seconds=(datetime.now(timezone.utc) - task.started_at).total_seconds(),
            )
            await self._events.publish(
                Event(
                    kind=EventKind.TASK_COMPLETED,
                    payload=attach_observability(
                        {"id": task.id, "cost_usd": task.cost_usd},
                        task_id=task.id,
                        backend=decision.backend_name,
                        model=decision.model,
                        cost_usd=task.cost_usd,
                        duration_seconds=(
                            datetime.now(timezone.utc) - task.started_at
                        ).total_seconds(),
                    ),
                )
            )
        except BudgetExceededError as e:
            log.warning("task %s refused: %s", task.id, e)
            task.status = TaskStatus.FAILED
            task.error = str(e)
            active_backend = task.backend or decision_backend
            active_model = task.model or decision_model
            record_request(
                backend=active_backend,
                model=active_model,
                status="budget_exceeded",
            )
            await self._events.publish(
                Event(
                    kind=EventKind.TASK_FAILED,
                    payload=attach_observability(
                        {
                            "id": task.id,
                            "error": str(e),
                            "reason": "budget_exceeded",
                        },
                        task_id=task.id,
                        backend=active_backend,
                        model=active_model,
                    ),
                )
            )
        except Exception as e:
            log.exception("task %s failed", task.id)
            task.status = TaskStatus.FAILED
            task.error = str(e)
            active_backend = task.backend or decision_backend
            active_model = task.model or decision_model
            record_request(backend=active_backend, model=active_model, status="error")
            await self._events.publish(
                Event(
                    kind=EventKind.TASK_FAILED,
                    payload=attach_observability(
                        {"id": task.id, "error": str(e)},
                        task_id=task.id,
                        backend=active_backend,
                        model=active_model,
                    ),
                )
            )
        finally:
            if repo_path:
                try:
                    from maxwell_daemon.daemon.workspace_hooks import (
                        execute_hooks,
                        load_hooks_config,
                    )

                    hook_config = load_hooks_config(repo_path, global_config=snapshot.config)
                    if hook_config:
                        await execute_hooks("after_run", repo_path, config=hook_config, fatal=False)
                except Exception as e:  # noqa: BLE001
                    log.warning("after_run hook failed (ignored): %s", e)
            task.finished_at = datetime.now(timezone.utc)
            try:
                self._memory.scratchpad.clear(task.id)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "scratchpad clear failed for task %s: %s",
                    task.id,
                    exc,
                    exc_info=True,
                )
            # Persist the final task state so restarts see exactly what the
            # daemon saw. Save rather than update_status because status may
            # have flipped more than once through the try/except chain.
            try:
                self._task_store.save(task)
            except Exception:
                # The task completed in-memory; log so operators can investigate
                # disk/lock issues, but don't alter the in-memory status because
                # the work result is already recorded on the Task object.
                log.exception("task store write failed for task=%s", task.id)
            if (
                self._memory is not None
                and hasattr(self._memory, "scratchpad")
                and getattr(self._memory, "scratchpad", None) is not None
            ):
                try:
                    self._memory.scratchpad.clear(task.id)
                except AttributeError:
                    # Scratchpad API mismatch — log but don't crash the task.
                    log.warning("scratchpad.clear API not available for task %s", task.id)

    async def _execute_issue(self, task: Task, decision: Any) -> None:
        """Run the issue → PR flow. Called with status already RUNNING."""
        from maxwell_daemon.core.repo_overrides import resolve_overrides
        from maxwell_daemon.gh import GitHubClient
        from maxwell_daemon.gh.executor import IssueExecutor
        from maxwell_daemon.gh.workspace import Workspace

        if task.issue_repo is None:
            raise ValueError(f"_execute_issue called for task {task.id!r} with no issue_repo set")
        if task.issue_number is None:
            raise ValueError(f"_execute_issue called for task {task.id!r} with no issue_number set")

        github = self._github_client or GitHubClient()
        workspace = self._workspace or Workspace(root=self._workspace_root)
        executor = (
            self._issue_executor_factory(github, workspace, decision.backend)
            if self._issue_executor_factory
            else IssueExecutor(
                github=github,
                workspace=workspace,
                backend=decision.backend,
                memory=self._memory,
                artifact_store=self._artifact_store,
            )
        )

        mode = task.issue_mode if task.issue_mode in {"plan", "implement"} else "plan"
        overrides = resolve_overrides(self._config, repo=task.issue_repo)

        # Smart model selection: if the task didn't specify a model AND the
        # backend has a tier_map, pick by issue complexity.

        effective_model = decision.model
        backend_cfg = self._router._backend_config(decision.backend_name)
        if not task.model and backend_cfg is not None and backend_cfg.tier_map:
            try:
                issue = await github.get_issue(task.issue_repo, task.issue_number)
                # Inject issue details into evaluator logic if needed,
                # or just use prompt length which for ISSUE is short.
                # Actually, pick_model_for_issue was better here because it has the issue object.
                # I'll keep pick_model_for_issue but use evaluator for budgeting.
                from maxwell_daemon.core.model_selector import pick_model_for_issue

                selection = pick_model_for_issue(
                    title=issue.title,
                    body=issue.body,
                    labels=list(issue.labels),
                    tier_map=backend_cfg.tier_map,
                    fallback=decision.model,
                )
                effective_model = selection.model
                log.info(
                    "model-select task=%s tier=%s model=%s factors=%s",
                    task.id,
                    selection.tier.value,
                    selection.model,
                    selection.factors,
                )
            except Exception:  # noqa: BLE001
                # Selection is opportunistic — a failure here falls through
                # to the default model so the task still proceeds.
                log.warning("model-select failed for task=%s; using default", task.id)

        task.backend = decision.backend_name
        task.model = effective_model
        task.route_reason = decision.reason
        try:
            self._task_store.save(task)
        except Exception:
            log.exception(
                "task store write failed while recording issue routing for task=%s",
                task.id,
            )

        # Record initial activity timestamp so stall detection doesn't trigger
        # before any output is produced.
        self._record_stream_event(task.id, EventKind.TASK_STARTED.value)

        async def _emit_test_output(chunk: str, stream: str) -> None:
            self._record_stream_event(task.id, f"{EventKind.TEST_OUTPUT.value}:{stream}")
            await self._events.publish(
                Event(
                    kind=EventKind.TEST_OUTPUT,
                    payload=attach_observability(
                        {
                            "task_id": task.id,
                            "chunk": chunk,
                            "stream": stream,
                        },
                        task_id=task.id,
                    ),
                )
            )

        result = await executor.execute_issue(
            repo=task.issue_repo,
            issue_number=task.issue_number,
            model=effective_model,
            mode=mode,  # type: ignore[arg-type]
            overrides=overrides,
            task_id=task.id,
            dry_run=task.dry_run,
            on_test_output=_emit_test_output,
        )

        task.status = TaskStatus.COMPLETED
        task.pr_url = result.pr_url
        task.result = result.plan
        # Issue-mode cost accounting is coarse — we don't see usage here since
        # the executor owns the backend call. Future: have the executor return
        # a usage object.
        record_request(
            backend=decision.backend_name,
            model=effective_model,
            status="success",
        )
        await self._events.publish(
            Event(
                kind=EventKind.TASK_COMPLETED,
                payload=attach_observability(
                    {
                        "id": task.id,
                        "kind": "issue",
                        "repo": task.issue_repo,
                        "issue": task.issue_number,
                        "pr_url": result.pr_url,
                    },
                    task_id=task.id,
                    backend=decision.backend_name,
                    model=effective_model,
                ),
            )
        )
