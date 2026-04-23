"""Daemon `submit_issue` — dispatches issues through the IssueExecutor collaborator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import TaskKind, TaskStatus


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
        github_client=object(),
        workspace=object(),
        executor_factory=lambda gh, ws, be: FakeExecutor(),
    )
    return d


async def _run_to_completion(daemon: Daemon, task_id: str, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        t = daemon.get_task(task_id)
        if t and t.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {task_id} did not finish: status={t.status if t else None}")


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
                assert final.status is TaskStatus.COMPLETED
                assert final.pr_url == "https://github.com/owner/repo/pull/999"
                assert "#42" in final.result
                assert final.backend == daemon_with_fake_executor._config.agent.default_backend
                assert (
                    final.model == daemon_with_fake_executor._config.backends[final.backend].model
                )
                assert final.route_reason == "global default"
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
                assert final.status is TaskStatus.COMPLETED
            finally:
                await daemon_with_fake_executor.stop()

        asyncio.run(body())
