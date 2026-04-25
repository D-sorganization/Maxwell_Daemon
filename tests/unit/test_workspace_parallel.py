"""Workspace isolation: per-task checkouts so concurrent agents don't collide."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from maxwell_daemon.gh.workspace import Workspace, WorkspaceError


class _RecordingGit:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    async def __call__(
        self,
        *argv: str,
        cwd: str | None = None,
        stdin: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        self.calls.append((argv, cwd))
        return 0, b"", b""


class TestPerTaskIsolation:
    def test_path_for_requires_task_id(self, tmp_path: Path) -> None:
        ws = Workspace(root=tmp_path)
        with pytest.raises(TypeError):
            # task_id is keyword-only and required — positional call must fail.
            ws.path_for("owner/repo")  # type: ignore[call-arg]

    def test_same_repo_different_tasks_get_different_paths(
        self, tmp_path: Path
    ) -> None:
        ws = Workspace(root=tmp_path)
        a = ws.path_for("owner/repo", task_id="task-a")
        b = ws.path_for("owner/repo", task_id="task-b")
        assert a != b
        assert "task-a" in str(a)
        assert "task-b" in str(b)

    def test_path_shape(self, tmp_path: Path) -> None:
        ws = Workspace(root=tmp_path)
        p = ws.path_for("owner/repo", task_id="task-abc123")
        assert p.parent.name == "repo"
        assert p.name == "task-abc123"

    def test_path_escape_rejected(self, tmp_path: Path) -> None:
        ws = Workspace(root=tmp_path)
        with pytest.raises(WorkspaceError):
            ws.path_for("owner/repo", task_id="../../etc")

    def test_task_id_validated(self, tmp_path: Path) -> None:
        ws = Workspace(root=tmp_path)
        for bad in ("", "has space", "has/slash", "has;semi"):
            with pytest.raises(WorkspaceError):
                ws.path_for("owner/repo", task_id=bad)


class TestEnsureCloneThreadsTaskId:
    def test_ensure_clone_writes_to_per_task_dir(self, tmp_path: Path) -> None:
        git = _RecordingGit()
        ws = Workspace(root=tmp_path, runner=git)

        asyncio.run(ws.ensure_clone("owner/repo", task_id="t-1"))

        clones = [c for c in git.calls if "clone" in c[0]]
        assert any("t-1" in arg for arg in clones[0][0])

    def test_concurrent_clones_do_not_overlap(self, tmp_path: Path) -> None:
        git = _RecordingGit()
        ws = Workspace(root=tmp_path, runner=git)

        async def run_both() -> None:
            await asyncio.gather(
                ws.ensure_clone("owner/repo", task_id="t-a"),
                ws.ensure_clone("owner/repo", task_id="t-b"),
            )

        asyncio.run(run_both())
        clone_dirs = [c[0][-1] for c in git.calls if "clone" in c[0]]
        assert len(set(clone_dirs)) == 2  # two distinct target paths


class TestBranchAndCommitThreadTaskId:
    def test_create_branch_uses_per_task_cwd(self, tmp_path: Path) -> None:
        # Seed the per-task directory so ensure_clone skips the actual clone.
        target = tmp_path / "repo" / "t-9"
        (target / ".git").mkdir(parents=True)

        git = _RecordingGit()
        ws = Workspace(root=tmp_path, runner=git)

        asyncio.run(
            ws.create_branch("owner/repo", "feature", base="main", task_id="t-9")
        )

        checkout_calls = [c for c in git.calls if "checkout" in c[0]]
        assert checkout_calls
        for _, cwd in checkout_calls:
            assert cwd is not None
            assert "t-9" in cwd


class TestCleanup:
    def test_cleanup_removes_old_workspaces(self, tmp_path: Path) -> None:
        ws = Workspace(root=tmp_path)
        old = tmp_path / "repo" / "old-task"
        new = tmp_path / "repo" / "new-task"
        old.mkdir(parents=True)
        new.mkdir(parents=True)

        # Backdate `old` beyond the retention window.
        stale_mtime = (datetime.now() - timedelta(days=10)).timestamp()
        import os

        os.utime(old, (stale_mtime, stale_mtime))

        removed = ws.cleanup_old(max_age=timedelta(days=7))
        assert old in removed
        assert new not in removed
        assert not old.exists()
        assert new.exists()

    def test_cleanup_returns_empty_when_all_fresh(self, tmp_path: Path) -> None:
        ws = Workspace(root=tmp_path)
        (tmp_path / "repo" / "fresh").mkdir(parents=True)
        assert ws.cleanup_old(max_age=timedelta(days=7)) == []
