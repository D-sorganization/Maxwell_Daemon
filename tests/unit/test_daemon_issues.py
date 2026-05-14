"""Daemon `submit_issue` — dispatches issues through the IssueExecutor collaborator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import TaskKind, TaskStatus
from maxwell_daemon.events import EventKind


class FakeIssue:
    def __init__(
        self, title: str = "Fix", body: str = "Fix", labels: list[str] | None = None
    ) -> None:
        self.title = title
        self.body = body
        self.labels = labels or ["bug"]


class FakeGithub:
    async def get_issue(self, repo: str, number: int) -> FakeIssue:
        return FakeIssue(labels=["complexity: high"])


class FakeExecutor:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    async def execute_issue(
        self,
        *,
        repo: str,
        issue_number: int,
        model: str,
        mode: str = "plan",
        **_: Any,
    ) -> Any:
        from maxwell_daemon.gh.executor import IssueResult

        return IssueResult(
            issue_number=issue_number,
            pr_url=f"https://github.com/{repo}/pull/999",
            pr_number=999,
            plan=f"plan for #{issue_number} via {model}",
            applied_diff=(mode == "implement"),
        )


class FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def ensure_clone(self, repo: str, *, task_id: str) -> Path:
        path = self.root / repo.replace("/", "__") / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path


@pytest.fixture
def daemon_with_fake_executor(
    minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path, tmp_path: Path
) -> Daemon:
    d = Daemon(
        minimal_config,
        ledger_path=isolated_ledger_path,
        workspace_root=tmp_path / "ws",
    )
    d.set_issue_collaborators(
        github_client=FakeGithub(),
        workspace=FakeWorkspace(tmp_path / "ws"),
        executor_factory=lambda gh, ws, be: FakeExecutor(),
    )
    return d


async def _run_to_completion(daemon: Daemon, task_id: str, timeout: float = 10.0) -> None:
    final_states = {TaskStatus.COMPLETED, TaskStatus.FAILED}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    task = daemon.get_task(task_id)
    while loop.time() < deadline:
        task = daemon.get_task(task_id)
        if task and task.status in final_states:
            return
        await asyncio.sleep(0.02)
    task = daemon.get_task(task_id)
    if task and task.status in final_states:
        return
    raise AssertionError(f"task {task_id} did not finish: status={task.status if task else None}")


def test_run_to_completion_checks_final_state_after_timeout() -> None:
    class CompletedTask:
        status = TaskStatus.COMPLETED

    class CompletedDaemon:
        def get_task(self, task_id: str) -> CompletedTask:
            return CompletedTask()

    asyncio.run(_run_to_completion(cast(Daemon, CompletedDaemon()), "finished", timeout=0.0))


class TestSubmitIssue:
    def test_creates_issue_kind_task(self, daemon_with_fake_executor: Daemon) -> None:
        t = daemon_with_fake_executor.submit_issue(repo="owner/repo", issue_number=42, mode="plan")
        assert t.kind is TaskKind.ISSUE
        assert t.issue_repo == "owner/repo"
        assert t.issue_number == 42
        assert t.issue_mode == "plan"

    def test_rejects_invalid_mode(self, daemon_with_fake_executor: Daemon) -> None:
        with pytest.raises(ValueError, match="mode"):
            daemon_with_fake_executor.submit_issue(repo="owner/repo", issue_number=1, mode="yolo")


class TestIssueDispatch:
    def test_issue_task_opens_pr(self, daemon_with_fake_executor: Daemon) -> None:
        async def body() -> None:
            await daemon_with_fake_executor.start(worker_count=1)
            try:
                task = daemon_with_fake_executor.submit_issue(
                    repo="owner/repo", issue_number=42, mode="plan"
                )
                await _run_to_completion(daemon_with_fake_executor, task.id)
                final = daemon_with_fake_executor.get_task(task.id)
                assert final.status is TaskStatus.COMPLETED  # type: ignore[union-attr]
                assert final.pr_url == "https://github.com/owner/repo/pull/999"  # type: ignore[union-attr]
                assert "#42" in final.result  # type: ignore[operator,union-attr]
                assert final.backend == daemon_with_fake_executor._config.agent.default_backend  # type: ignore[union-attr]
                assert (
                    final.model == daemon_with_fake_executor._config.backends[final.backend].model  # type: ignore[union-attr]
                )
                assert final.route_reason == "global default"  # type: ignore[union-attr]
            finally:
                await daemon_with_fake_executor.stop()

        asyncio.run(body())

    def test_implement_mode_marked(self, daemon_with_fake_executor: Daemon) -> None:
        async def body() -> None:
            await daemon_with_fake_executor.start(worker_count=1)
            try:
                task = daemon_with_fake_executor.submit_issue(
                    repo="owner/repo", issue_number=1, mode="implement"
                )
                await _run_to_completion(daemon_with_fake_executor, task.id)
                final = daemon_with_fake_executor.get_task(task.id)
                assert final.status is TaskStatus.COMPLETED  # type: ignore[union-attr]
            finally:
                await daemon_with_fake_executor.stop()

        asyncio.run(body())

    def test_issue_completion_event_uses_effective_model(
        self, daemon_with_fake_executor: Daemon
    ) -> None:
        async def body() -> None:
            # Configure a tier_map that maps "complex" to "override-model"
            backend_cfg = daemon_with_fake_executor._config.backends[
                daemon_with_fake_executor._config.agent.default_backend
            ]
            backend_cfg.tier_map = {"complex": "override-model"}

            events = []

            async def drain_events() -> None:
                sub = daemon_with_fake_executor._events.subscribe()
                try:
                    async for ev in sub:
                        if ev.kind == EventKind.TASK_COMPLETED:
                            events.append(ev)
                except asyncio.CancelledError:
                    pass

            drain_task = asyncio.create_task(drain_events())

            await daemon_with_fake_executor.start(worker_count=1)
            try:
                task = daemon_with_fake_executor.submit_issue(
                    repo="owner/repo", issue_number=42, mode="plan"
                )
                await _run_to_completion(daemon_with_fake_executor, task.id)
                final = daemon_with_fake_executor.get_task(task.id)
                assert final.status is TaskStatus.COMPLETED  # type: ignore[union-attr]

                # Give the event loop a chance to propagate events
                await asyncio.sleep(0.1)

                assert len(events) == 1
                payload = events[0].payload
                assert payload.get("observability", {}).get("model") == "override-model"
                assert final.model == "override-model"  # type: ignore[union-attr]
            finally:
                drain_task.cancel()
                await daemon_with_fake_executor.stop()

        asyncio.run(body())
