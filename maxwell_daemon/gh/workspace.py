"""Local-filesystem workspace for per-task repo clones.

Each running agent gets its own clone of the target repo keyed by task id, so
two concurrent agents targeting the same repo never share a working directory.
Every git invocation goes through an injectable runner so tests never touch a
real filesystem worktree.

Path layout::

    <root>/<repo-name>/<task-id>/   ← one working tree per task

Security
--------
* ``asyncio.create_subprocess_exec`` with argv lists; no shell.
* ``repo`` and ``task_id`` are regex-validated.
* ``path_for`` resolves its output and refuses paths that escape the root.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

__all__ = ["Workspace", "WorkspaceError"]

# Must match maxwell_daemon.gh.client._REPO_RE exactly — kept in sync by the test suite.
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
# Task ids are short hex / dash identifiers (uuid4().hex[:12] by default).
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")

RunnerFn = Callable[..., Awaitable[tuple[int, bytes, bytes]]]


class WorkspaceError(RuntimeError):
    """Raised when a git operation against the workspace fails."""


async def _default_runner(
    *argv: str,
    cwd: str | None = None,
    stdin: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=stdin)
    return proc.returncode or 0, stdout, stderr


class Workspace:
    """Encapsulates a directory of per-task repo checkouts under a shared root."""

    def __init__(self, root: Path, *, runner: RunnerFn | None = None) -> None:
        self._root = Path(root).expanduser()
        self._root.mkdir(parents=True, exist_ok=True)
        self._run = runner or _default_runner

    def path_for(self, repo: str, *, task_id: str) -> Path:
        """Resolve the on-disk path for ``(repo, task_id)``.

        Both inputs are regex-validated and the final path is resolved and
        checked against the root to prevent traversal attacks.
        """
        if not _REPO_RE.match(repo):
            raise WorkspaceError(f"Invalid repo {repo!r}")
        if not _TASK_ID_RE.match(task_id):
            raise WorkspaceError(f"Invalid task id {task_id!r}")
        repo_dir = self._root / repo.split("/", 1)[1]
        target = (repo_dir / task_id).resolve()
        root_resolved = self._root.resolve()
        if root_resolved not in target.parents and target != root_resolved:
            raise WorkspaceError(f"Path escape detected for repo={repo!r} task={task_id!r}")
        return target

    async def _run_git(
        self, *argv: str, cwd: Path | None = None, stdin: bytes | None = None
    ) -> None:
        rc, _, err = await self._run("git", *argv, cwd=str(cwd) if cwd else None, stdin=stdin)
        if rc != 0:
            raise WorkspaceError(
                f"git {' '.join(argv)} failed: {err.decode(errors='replace').strip()}"
            )

    async def ensure_clone(self, repo: str, *, task_id: str, depth: int = 50) -> Path:
        """Clone ``repo`` into the per-task directory if absent; otherwise fetch."""
        target = self.path_for(repo, task_id=task_id)
        if (target / ".git").exists():
            await self._run_git("fetch", "--all", "--prune", cwd=target)
            return target

        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{repo}.git"
        await self._run_git("clone", "--depth", str(depth), url, str(target))
        return target

    async def create_branch(
        self, repo: str, branch: str, *, base: str = "main", task_id: str
    ) -> None:
        target = self.path_for(repo, task_id=task_id)
        await self._run_git("checkout", base, cwd=target)
        await self._run_git("pull", "--ff-only", "origin", base, cwd=target)
        await self._run_git("checkout", "-B", branch, cwd=target)

    async def apply_diff(self, repo: str, diff: str, *, task_id: str) -> None:
        target = self.path_for(repo, task_id=task_id)
        await self._run_git("apply", "--index", "-", cwd=target, stdin=diff.encode())

    async def commit_and_push(self, repo: str, *, branch: str, message: str, task_id: str) -> None:
        target = self.path_for(repo, task_id=task_id)
        await self._run_git("add", "-A", cwd=target)
        await self._run_git("commit", "-m", message, cwd=target)
        await self._run_git("push", "--set-upstream", "origin", branch, cwd=target)

    def cleanup_old(self, *, max_age: timedelta) -> list[Path]:
        """Remove per-task checkouts older than ``max_age``. Returns what was removed.

        Intended for a cron/systemd-timer job — the daemon itself doesn't prune
        so concurrent task cleanup can never race an in-flight task.
        """
        now = datetime.now().timestamp()
        cutoff = now - max_age.total_seconds()
        removed: list[Path] = []
        if not self._root.is_dir():
            return removed
        for repo_dir in self._root.iterdir():
            if not repo_dir.is_dir():
                continue
            for task_dir in repo_dir.iterdir():
                if not task_dir.is_dir():
                    continue
                if task_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(task_dir, ignore_errors=True)
                    removed.append(task_dir)
        return removed
