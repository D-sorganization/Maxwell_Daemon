"""Execution Sandbox for safe, ephemeral task isolation.

Adheres strictly to disk space conservation and system security.
Uses Docker with the `--rm` flag to guarantee container destruction
immediately after the task completes, preventing orphaned containers
from consuming disk space.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str


class ExecutionSandbox:
    """Provides a safe, isolated execution environment using Docker."""

    def __init__(
        self, image: str = "python:3.13-slim", workspace_root: Path | str | None = None
    ) -> None:
        self.image = image
        self.workspace_root = (
            Path(workspace_root).expanduser().resolve() if workspace_root else None
        )

    async def run_command(self, cmd: str, timeout: int = 60) -> SandboxResult:
        """Executes a command inside the ephemeral sandbox."""
        mount_args: list[str] = []
        worktree_context = None

        if self.workspace_root:
            is_git_repo = False
            with contextlib.suppress(OSError):
                is_git_repo = (self.workspace_root / ".git").exists()

            if is_git_repo:
                from maxwell_daemon.sandbox.git import GitTracker, GitWorktree

                tracker = GitTracker(self.workspace_root)
                try:
                    snapshot_id = tracker.take_snapshot()
                except (subprocess.SubprocessError, OSError):
                    snapshot_id = "HEAD"
                worktree_context = GitWorktree(self.workspace_root, commit_ish=snapshot_id)

        if worktree_context:
            try:
                async with worktree_context as worktree_path:
                    mount_args = ["-v", f"{worktree_path}:/workspace", "-w", "/workspace"]
                    return await self._run_docker(cmd, mount_args, timeout)
            except Exception as e:  # noqa: BLE001
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Failed to setup Git worktree for Docker: {e!s}",
                )
        else:
            if self.workspace_root:
                mount_args = ["-v", f"{self.workspace_root}:/workspace", "-w", "/workspace"]
            return await self._run_docker(cmd, mount_args, timeout)

    async def _run_docker(self, cmd: str, mount_args: list[str], timeout: int) -> SandboxResult:
        # DbC: The contract here is absolute isolation and guaranteed cleanup.
        # --rm ensures no disk space leaks.
        # --network none ensures the agent cannot make rogue API calls.
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
        ]
        docker_cmd.extend(mount_args)
        docker_cmd.extend(
            [
                self.image,
                "sh",
                "-c",
                cmd,
            ]
        )

        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            stdout_bytes, stderr_bytes = await proc.communicate()
            return SandboxResult(
                exit_code=-1,
                stdout=stdout_bytes.decode(errors="replace"),
                stderr="Execution timed out",
            )

        return SandboxResult(
            exit_code=proc.returncode or 0,
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
        )
