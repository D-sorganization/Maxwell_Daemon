"""Local-filesystem workspace for a GitHub repo clone.

Used by the IssueExecutor to check out a repo, branch off main, apply an
LLM-produced diff, commit, and push. Every git invocation goes through an
injectable runner so tests never touch a real filesystem worktree.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

__all__ = ["Workspace", "WorkspaceError"]

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
    """Encapsulates a directory of checked-out repos under a shared root."""

    def __init__(self, root: Path, *, runner: RunnerFn | None = None) -> None:
        self._root = Path(root).expanduser()
        self._root.mkdir(parents=True, exist_ok=True)
        self._run = runner or _default_runner

    def path_for(self, repo: str) -> Path:
        # One directory per repo. Name is the repo's path segment, not owner/name,
        # so we don't create nested owner/ directories.
        return self._root / repo.split("/", 1)[1]

    async def _run_git(
        self, *argv: str, cwd: Path | None = None, stdin: bytes | None = None
    ) -> None:
        rc, _, err = await self._run("git", *argv, cwd=str(cwd) if cwd else None, stdin=stdin)
        if rc != 0:
            raise WorkspaceError(
                f"git {' '.join(argv)} failed: {err.decode(errors='replace').strip()}"
            )

    async def ensure_clone(self, repo: str, *, depth: int = 50) -> Path:
        """Clone the repo if absent, otherwise fetch updates."""
        target = self.path_for(repo)
        if (target / ".git").exists():
            await self._run_git("fetch", "--all", "--prune", cwd=target)
            return target

        # Subprocess doesn't run a shell, so the URL is data, not code — but
        # we still validated the repo string upstream.
        url = f"https://github.com/{repo}.git"
        await self._run_git("clone", "--depth", str(depth), url, str(target))
        return target

    async def create_branch(self, repo: str, branch: str, *, base: str = "main") -> None:
        target = self.path_for(repo)
        await self._run_git("checkout", base, cwd=target)
        await self._run_git("pull", "--ff-only", "origin", base, cwd=target)
        await self._run_git("checkout", "-B", branch, cwd=target)

    async def apply_diff(self, repo: str, diff: str) -> None:
        target = self.path_for(repo)
        # `--index` stages the changes so they're included in the next commit.
        await self._run_git("apply", "--index", "-", cwd=target, stdin=diff.encode())

    async def commit_and_push(self, repo: str, *, branch: str, message: str) -> None:
        target = self.path_for(repo)
        await self._run_git("add", "-A", cwd=target)
        await self._run_git("commit", "-m", message, cwd=target)
        await self._run_git("push", "--set-upstream", "origin", branch, cwd=target)
