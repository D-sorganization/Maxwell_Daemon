"""Issues, templates, memory, and artifact endpoints.

Extracted from ``maxwell_daemon/api/server.py`` as part of epic #896
Phase 1.1.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, cast

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from maxwell_daemon.api.validation import PriorityField, RepoField
from maxwell_daemon.core.artifacts import ArtifactIntegrityError
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import Task
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "AssembleMemoryRequest",
    "IssueAbDispatch",
    "IssueBatchDispatch",
    "IssueCreate",
    "IssueDispatch",
    "IssueTaskView",
    "RecordMemoryOutcome",
    "register",
]


class AssembleMemoryRequest(BaseModel):
    repo: str = Field(..., min_length=1)
    issue_title: str = ""
    issue_body: str = ""
    task_id: str = Field(..., min_length=1)
    max_chars: int = 8000


class RecordMemoryOutcome(BaseModel):
    task_id: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    issue_number: int
    issue_title: str = ""
    issue_body: str = ""
    plan: str = ""
    applied_diff: bool = False
    pr_url: str = ""
    outcome: str = ""


class IssueCreate(BaseModel):
    repo: RepoField
    title: str = Field(..., min_length=1)
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    dispatch: bool = False
    mode: str = Field(default="plan", pattern=r"^(plan|implement)$")


class IssueDispatch(BaseModel):
    repo: RepoField
    number: int = Field(..., ge=1)
    mode: str = Field(default="plan", pattern=r"^(plan|implement)$")
    backend: str | None = None
    model: str | None = None
    priority: PriorityField = 100
    dry_run: bool = False


class IssueBatchDispatch(BaseModel):
    items: list[IssueDispatch] = Field(..., min_length=1, max_length=100)


class IssueAbDispatch(BaseModel):
    repo: RepoField
    number: int = Field(..., ge=1)
    backends: list[str] = Field(..., min_length=2, max_length=4)
    mode: str = Field(default="plan", pattern=r"^(plan|implement)$")
    dry_run: bool = False

    @field_validator("backends")
    @classmethod
    def _check_distinct(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("A/B backends must be distinct")
        return v


class IssueTaskView(BaseModel):
    id: str
    prompt: str
    kind: str
    repo: str | None
    backend: str | None
    model: str | None
    route_reason: str | None = None
    issue_repo: str | None = None
    issue_number: int | None = None
    issue_mode: str | None = None
    ab_group: str | None = None
    thread_id: str | None = None
    turn_count: int = 0
    max_turns: int = 20
    session_id: str
    continuation: bool = False
    depends_on: list[str] = Field(default_factory=list)
    priority: int = 100
    pr_url: str | None = None
    dispatched_to: str | None = None
    side_effects_started: bool = False
    status: str
    result: str | None
    error: str | None
    waived_by: str | None = None
    waiver_reason: str | None = None
    waived_at: datetime | None = None
    dry_run: bool = False
    cost_usd: float
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_task(cls, t: Task) -> IssueTaskView:
        return cls(
            id=t.id,
            prompt=t.prompt,
            kind=t.kind.value,
            repo=t.repo,
            backend=t.backend,
            model=t.model,
            route_reason=t.route_reason,
            issue_repo=t.issue_repo,
            issue_number=t.issue_number,
            issue_mode=t.issue_mode,
            ab_group=t.ab_group,
            thread_id=t.thread_id,
            turn_count=t.turn_count,
            max_turns=t.max_turns,
            session_id=t.turn_session_id,
            continuation=t.is_continuation_turn,
            depends_on=list(getattr(t, "depends_on", [])),
            priority=getattr(t, "priority", 100),
            pr_url=t.pr_url,
            dispatched_to=t.dispatched_to,
            side_effects_started=t.side_effects_started,
            status=t.status.value,
            result=t.result,
            error=t.error,
            waived_by=t.waived_by,
            waiver_reason=t.waiver_reason,
            waived_at=t.waived_at,
            cost_usd=t.cost_usd,
            created_at=t.created_at,
            started_at=t.started_at,
            finished_at=t.finished_at,
            dry_run=getattr(t, "dry_run", False),
        )


def register(  # noqa: C901
    app: FastAPI,
    daemon: Daemon,
    gh: Any,
    require_viewer: Any,
    require_operator: Any,
    require_admin: Any,
    auth: Any,
) -> None:
    """Attach issues, templates, memory, and artifact endpoints to ``app``."""

    # -- Memory --

    @app.post(
        "/api/v1/memory/assemble",
        dependencies=[Depends(auth), Depends(require_operator)],
    )
    async def assemble_memory(payload: AssembleMemoryRequest) -> dict[str, Any]:
        """Assemble repo/task context from the coordinator's shared memory store."""
        context = daemon._memory.assemble_context(
            repo=payload.repo,
            issue_title=payload.issue_title,
            issue_body=payload.issue_body,
            task_id=payload.task_id,
            max_chars=payload.max_chars,
        )
        return {"context": context}

    @app.post(
        "/api/v1/memory/record",
        dependencies=[Depends(auth), Depends(require_operator)],
        status_code=status.HTTP_201_CREATED,
    )
    async def record_memory(payload: RecordMemoryOutcome) -> dict[str, Any]:
        """Record a completed task's outcome to the coordinator's shared memory store."""
        daemon._memory.record_outcome(
            task_id=payload.task_id,
            repo=payload.repo,
            issue_number=payload.issue_number,
            issue_title=payload.issue_title,
            issue_body=payload.issue_body,
            plan=payload.plan,
            applied_diff=payload.applied_diff,
            pr_url=payload.pr_url,
            outcome=payload.outcome,
        )
        return {"status": "recorded"}

    # -- Issues --

    @app.post(
        "/api/v1/issues",
        dependencies=[Depends(auth), Depends(require_operator)],
        status_code=status.HTTP_201_CREATED,
    )
    async def create_issue(payload: IssueCreate) -> dict[str, Any]:
        """Create a GitHub issue. Optionally dispatch the daemon immediately."""
        _gh = gh()
        url = await _gh.create_issue(
            payload.repo,
            title=payload.title,
            body=payload.body,
            labels=payload.labels or None,
        )
        result: dict[str, Any] = {"url": url}

        if payload.dispatch:
            match = re.search(r"/issues/(\d+)/?\s*$", url.strip())
            if match:
                number = int(match.group(1))
                task = daemon.submit_issue(
                    repo=payload.repo, issue_number=number, mode=payload.mode
                )
                result["task_id"] = task.id
        return result

    @app.post(
        "/api/v1/issues/dispatch",
        dependencies=[Depends(auth), Depends(require_admin)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def dispatch_issue(payload: IssueDispatch) -> IssueTaskView:
        """Queue an existing issue for the daemon to draft a PR for."""
        task = daemon.submit_issue(
            repo=payload.repo,
            issue_number=payload.number,
            mode=payload.mode,
            backend=payload.backend,
            model=payload.model,
            priority=payload.priority,
            dry_run=payload.dry_run,
        )
        return IssueTaskView.from_task(task)

    @app.post(
        "/api/v1/issues/ab-dispatch",
        dependencies=[Depends(auth), Depends(require_operator)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def ab_dispatch_issue(payload: IssueAbDispatch) -> dict[str, Any]:
        """Race multiple backends on the same issue — reviewer picks the winner."""
        available = set(daemon.state().backends_available)
        unknown = [b for b in payload.backends if b not in available]
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"unknown backend(s): {', '.join(unknown)}; available: {sorted(available)}",
            )
        try:
            tasks = daemon.submit_issue_ab(
                repo=payload.repo,
                issue_number=payload.number,
                backends=payload.backends,
                mode=payload.mode,
                dry_run=payload.dry_run,
            )
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
        return {
            "ab_group": tasks[0].ab_group,
            "tasks": [IssueTaskView.from_task(t).model_dump(mode="json") for t in tasks],
        }

    @app.post(
        "/api/v1/issues/batch-dispatch",
        dependencies=[Depends(auth), Depends(require_operator)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def batch_dispatch_issues(payload: IssueBatchDispatch) -> dict[str, Any]:
        """Queue up to 100 issues in one call."""
        dispatched: list[IssueTaskView] = []
        failures: list[dict[str, Any]] = []
        for item in payload.items:
            try:
                task = daemon.submit_issue(
                    repo=item.repo,
                    issue_number=item.number,
                    mode=item.mode,
                    backend=item.backend,
                    model=item.model,
                    priority=item.priority,
                )
                dispatched.append(IssueTaskView.from_task(task))
            except Exception as e:  # noqa: BLE001
                failures.append({"repo": item.repo, "number": item.number, "error": str(e)})
        return {
            "dispatched": len(dispatched),
            "failed": len(failures),
            "tasks": [t.model_dump(mode="json") for t in dispatched],
            "failures": failures,
        }

    @app.get("/api/v1/issues/{owner}/{name}", dependencies=[Depends(require_viewer)])
    async def list_repo_issues(
        owner: str, name: str, state: str = "open", limit: int = 25
    ) -> list[dict[str, Any]]:
        _gh = gh()
        issues = await _gh.list_issues(f"{owner}/{name}", state=state, limit=limit)
        return [
            {
                "number": i.number,
                "title": i.title,
                "state": i.state,
                "labels": i.labels,
                "url": i.url,
            }
            for i in issues
        ]

    # -- Templates --

    @app.get("/api/v1/templates", dependencies=[Depends(require_viewer)])
    async def list_templates() -> list[dict[str, Any]]:
        """List all available task templates."""
        return [t.model_dump() for t in daemon.template_store.list_templates()]

    @app.get("/api/v1/templates/{template_id}", dependencies=[Depends(require_viewer)])
    async def get_template(template_id: str) -> dict[str, Any]:
        """Fetch a specific task template by ID."""
        t = daemon.template_store.get_template(template_id)
        if not t:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Template {template_id} not found")
        return cast(dict[str, Any], t.model_dump())

    # -- Artifacts --

    @app.get("/api/v1/artifacts/{artifact_id}", dependencies=[Depends(require_viewer)])
    async def get_artifact(artifact_id: str) -> dict[str, Any]:
        from maxwell_daemon.api.routes.actions import ArtifactView

        artifact = daemon.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
        return ArtifactView.from_artifact(artifact).model_dump()

    @app.get(
        "/api/v1/artifacts/{artifact_id}/content",
        dependencies=[Depends(require_viewer)],
    )
    async def get_artifact_content(artifact_id: str) -> Any:
        from fastapi import Response

        artifact = daemon.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
        try:
            content = daemon.read_artifact_bytes(artifact_id)
        except ArtifactIntegrityError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return Response(content=content, media_type=artifact.media_type)
