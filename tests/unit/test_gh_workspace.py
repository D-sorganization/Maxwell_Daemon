"""Workspace — clone/fetch repo, create branch, apply changes, push.

Tested by substituting the subprocess runner. No actual git invoked here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from maxwell_daemon.gh.workspace import Workspace, WorkspaceError


class FakeGit:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self._responses: dict[tuple[str, ...], tuple[int, bytes, bytes]] = {}

    def respond(
        self,
        *argv: str,
        returncode: int = 0,
        stdout: bytes | str = b"",
        stderr: bytes | str = b"",
    ) -> None:
        if isinstance(stdout, str):
            stdout = stdout.encode()
        if isinstance(stderr, str):
            stderr = stderr.encode()
        self._responses[argv] = (returncode, stdout, stderr)

    async def __call__(
        self,
        *argv: str,
        cwd: str | None = None,
        stdin: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        self.calls.append((argv, cwd))
        return self._responses.get(argv, (0, b"", b""))


class TestClone:
    def test_clones_when_absent(self, tmp_path: Path) -> None:
        git = FakeGit()
        ws = Workspace(root=tmp_path, runner=git)
        asyncio.run(ws.ensure_clone("owner/repo", task_id="t-1"))
        assert any("clone" in call[0] for call in git.calls)

    def test_fetches_when_present(self, tmp_path: Path) -> None:
        # Seed the per-task checkout directory.
        (tmp_path / "repo" / "t-1" / ".git").mkdir(parents=True)
        git = FakeGit()
        ws = Workspace(root=tmp_path, runner=git)
        asyncio.run(ws.ensure_clone("owner/repo", task_id="t-1"))
        cmds = [c[0] for c in git.calls]
        assert any("fetch" in c for c in cmds)
        assert not any("clone" in c for c in cmds)

    def test_clone_failure_raises(self, tmp_path: Path) -> None:
        git = FakeGit()
        git.respond(
            "git",
            "clone",
            "--depth",
            "50",
            "https://github.com/owner/repo.git",
            str((tmp_path / "repo" / "t-1").resolve()),
            returncode=128,
            stderr=b"Repository not found",
        )
        ws = Workspace(root=tmp_path, runner=git)
        with pytest.raises(WorkspaceError, match="Repository not found"):
            asyncio.run(ws.ensure_clone("owner/repo", task_id="t-1"))


class TestBranchLifecycle:
    def test_create_branch_from_main(self, tmp_path: Path) -> None:
        (tmp_path / "repo" / "t-1" / ".git").mkdir(parents=True)
        git = FakeGit()
        ws = Workspace(root=tmp_path, runner=git)
        asyncio.run(
            ws.create_branch("owner/repo", "maxwell-daemon/issue-42", base="main", task_id="t-1")
        )
        cmds = [c[0] for c in git.calls]
        assert ("git", "checkout", "main") in cmds
        assert ("git", "checkout", "-B", "maxwell-daemon/issue-42") in cmds

    def test_commit_and_push(self, tmp_path: Path) -> None:
        (tmp_path / "repo" / "t-1" / ".git").mkdir(parents=True)
        git = FakeGit()
        ws = Workspace(root=tmp_path, runner=git)
        asyncio.run(
            ws.commit_and_push(
                "owner/repo",
                branch="maxwell-daemon/issue-42",
                message="Fix #42",
                task_id="t-1",
            )
        )
        cmds = [c[0] for c in git.calls]
        assert ("git", "add", "-A") in cmds
        assert ("git", "commit", "-m", "Fix #42") in cmds
        assert ("git", "push", "--set-upstream", "origin", "maxwell-daemon/issue-42") in cmds


class TestApplyDiff:
    def test_applies_valid_patch(self, tmp_path: Path) -> None:
        (tmp_path / "repo" / "t-1" / ".git").mkdir(parents=True)
        git = FakeGit()
        git.respond("git", "apply", "--index", "-", returncode=0)
        ws = Workspace(root=tmp_path, runner=git)
        diff = "diff --git a/x b/x\n@@ -0,0 +1 @@\n+new\n"
        asyncio.run(ws.apply_diff("owner/repo", diff, task_id="t-1"))
        cmds = [c[0] for c in git.calls]
        assert ("git", "apply", "--index", "-") in cmds

    def test_propagates_apply_failure(self, tmp_path: Path) -> None:
        (tmp_path / "repo" / "t-1" / ".git").mkdir(parents=True)
        git = FakeGit()
        git.respond(
            "git",
            "apply",
            "--index",
            "-",
            returncode=1,
            stderr=b"patch does not apply",
        )
        ws = Workspace(root=tmp_path, runner=git)
        with pytest.raises(WorkspaceError, match="does not apply"):
            asyncio.run(ws.apply_diff("owner/repo", "garbage", task_id="t-1"))
