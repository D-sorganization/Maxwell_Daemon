"""Execution Sandbox for safe, ephemeral task isolation.

Adheres strictly to disk space conservation and system security.
Uses Docker with the `--rm` flag to guarantee container destruction
immediately after the task completes, preventing orphaned containers
from consuming disk space.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(slots=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str


class ExecutionSandbox:
    """Provides a safe, isolated execution environment using Docker."""

    def __init__(self, image: str = "python:3.13-slim") -> None:
        self.image = image

    async def run_command(self, cmd: str, timeout: int = 60) -> SandboxResult:
        """Executes a command inside the ephemeral sandbox."""

        # DbC: The contract here is absolute isolation and guaranteed cleanup.
        # --rm ensures no disk space leaks.
        # --network none ensures the agent cannot make rogue API calls.
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            self.image,
            "sh",
            "-c",
            cmd,
        ]

        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
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
