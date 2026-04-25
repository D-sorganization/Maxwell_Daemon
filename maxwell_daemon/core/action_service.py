"""Service boundary for proposing and deciding agent actions."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from maxwell_daemon.audit import AuditLogger
from maxwell_daemon.core.action_policy import ActionPolicy, PolicyDecision
from maxwell_daemon.core.action_store import ActionStore
from maxwell_daemon.core.actions import Action, ActionKind, ActionRiskLevel, ActionStatus
from maxwell_daemon.events import Event, EventBus, EventKind, attach_observability
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

_ACTION_EXECUTOR_TIMEOUT_SECONDS = 120.0


class ActionTimeoutError(Exception):
    """Raised when an action executor exceeds its allotted wall-clock time."""


class ActionService:
    def __init__(
        self,
        store: ActionStore,
        *,
        policy: ActionPolicy | None = None,
        events: EventBus | None = None,
    ) -> None:
        self._store = store
        self._policy = policy or ActionPolicy()
        self._events = events
        self._event_tasks: set[asyncio.Task[None]] = set()

    def propose(
        self,
        *,
        task_id: str,
        kind: ActionKind,
        summary: str,
        payload: dict[str, Any] | None = None,
        work_item_id: str | None = None,
        risk_level: ActionRiskLevel = ActionRiskLevel.MEDIUM,
        dry_run: bool = False,
    ) -> tuple[Action, PolicyDecision]:
        action = Action(
            id=uuid.uuid4().hex,
            task_id=task_id,
            work_item_id=work_item_id,
            kind=kind,
            summary=summary,
            payload=payload or {},
            risk_level=risk_level,
        )
        decision = self._policy.evaluate(action, dry_run=dry_run)
        action.requires_approval = decision.requires_approval
        self._store.save(action)
        self._publish(EventKind.ACTION_PROPOSED, action)
        return action, decision

    async def propose_and_maybe_run(
        self,
        *,
        task_id: str,
        kind: ActionKind,
        summary: str,
        payload: dict[str, Any] | None,
        runner: Callable[[], Awaitable[dict[str, Any]] | dict[str, Any]],
        work_item_id: str | None = None,
        risk_level: ActionRiskLevel = ActionRiskLevel.MEDIUM,
        dry_run: bool = False,
    ) -> Action:
        action, decision = self.propose(
            task_id=task_id,
            kind=kind,
            summary=summary,
            payload=payload,
            work_item_id=work_item_id,
            risk_level=risk_level,
            dry_run=dry_run,
        )
        if decision.requires_approval:
            return action
        if not decision.allowed:
            return self.skip(action.id, reason=decision.reason)
        approved = self.approve(action.id, actor="policy")
        running = self.mark_running(approved.id)
        try:
            result = runner()
            if inspect.isawaitable(result):
                try:
                    result = await asyncio.wait_for(
                        result, timeout=_ACTION_EXECUTOR_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    log.error(
                        "Action %s timed out after %.0fs",
                        action.id,
                        _ACTION_EXECUTOR_TIMEOUT_SECONDS,
                    )
                    raise ActionTimeoutError(
                        f"Action {action.id} exceeded {_ACTION_EXECUTOR_TIMEOUT_SECONDS:.0f}s timeout"
                    ) from None
            return self.mark_applied(running.id, result=result)
        except Exception as exc:
            return self.mark_failed(running.id, error=str(exc))

    def get(self, action_id: str) -> Action | None:
        return self._store.get(action_id)

    def list_for_task(self, task_id: str) -> list[Action]:
        return self._store.list_for_task(task_id)

    def list(
        self,
        *,
        status: ActionStatus | None = None,
        task_id: str | None = None,
        work_item_id: str | None = None,
        limit: int = 100,
    ) -> list[Action]:
        return self._store.list(
            status=status,
            task_id=task_id,
            work_item_id=work_item_id,
            limit=limit,
        )

    def approve(
        self,
        action_id: str,
        *,
        actor: str,
        audit: AuditLogger | None = None,
    ) -> Action:
        action = self._store.transition(action_id, ActionStatus.APPROVED, actor=actor)
        if audit is not None:
            audit.log_agent_operation(
                operation="action_approved",
                task_id=action.task_id,
                details={"action_id": action.id, "actor": actor, "kind": action.kind.value},
            )
        self._publish(EventKind.ACTION_APPROVED, action)
        return action

    def reject(
        self,
        action_id: str,
        *,
        actor: str,
        reason: str | None = None,
        audit: AuditLogger | None = None,
    ) -> Action:
        action = self._store.transition(
            action_id,
            ActionStatus.REJECTED,
            actor=actor,
            reason=reason,
        )
        if audit is not None:
            audit.log_agent_operation(
                operation="action_rejected",
                task_id=action.task_id,
                details={
                    "action_id": action.id,
                    "actor": actor,
                    "kind": action.kind.value,
                    "reason": reason,
                },
            )
        self._publish(EventKind.ACTION_REJECTED, action)
        return action

    def skip(self, action_id: str, *, reason: str | None = None) -> Action:
        action = self._store.transition(action_id, ActionStatus.SKIPPED, reason=reason)
        self._publish(EventKind.ACTION_SKIPPED, action)
        return action

    def mark_running(self, action_id: str) -> Action:
        action = self._store.transition(action_id, ActionStatus.RUNNING)
        self._publish(EventKind.ACTION_RUNNING, action)
        return action

    def mark_applied(
        self,
        action_id: str,
        *,
        result: dict[str, Any] | None = None,
        result_artifact_id: str | None = None,
        inverse_payload: dict[str, Any] | None = None,
    ) -> Action:
        action = self._store.transition(
            action_id,
            ActionStatus.APPLIED,
            result=result,
            result_artifact_id=result_artifact_id,
            inverse_payload=inverse_payload,
        )
        self._publish(EventKind.ACTION_APPLIED, action)
        return action

    def mark_failed(self, action_id: str, *, error: str) -> Action:
        action = self._store.transition(action_id, ActionStatus.FAILED, error=error)
        self._publish(EventKind.ACTION_FAILED, action)
        return action

    def mark_reverted(self, action_id: str) -> Action:
        action = self._store.transition(action_id, ActionStatus.REVERTED)
        # Assuming we don't have ACTION_REVERTED event yet, we could just not publish or add one.
        # But wait, we don't have EventKind.ACTION_REVERTED. Let's look at events.py if we need to publish.
        return action

    def _publish(self, kind: EventKind, action: Action) -> None:
        if self._events is None:
            return
        payload = {
            "action_id": action.id,
            "task_id": action.task_id,
            "work_item_id": action.work_item_id,
            "kind": action.kind.value,
            "status": action.status.value,
            "summary": action.summary,
        }
        payload = attach_observability(
            payload,
            task_id=action.task_id,
            work_item_id=action.work_item_id,
            action_id=action.id,
        )
        try:
            loop = asyncio.get_running_loop()
            if len(self._event_tasks) >= 1000:
                log.warning("event_tasks queue saturated, dropping action event %s", kind.value)
                return
            task = loop.create_task(self._events.publish(Event(kind=kind, payload=payload)))
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)
        except RuntimeError:
            return
