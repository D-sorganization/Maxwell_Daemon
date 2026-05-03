"""Workspace lifecycle hooks implementation based on Symphony SPEC.md §5.3.4.

Defines the hook configurations and the execution logic for workspace
lifecycle events (after_create, before_run, after_run, before_remove).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maxwell_daemon.config import MaxwellDaemonConfig

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceHooksConfig:
    """Configuration for workspace lifecycle hooks."""

    timeout_ms: int = 60000
    after_create: list[str] = field(default_factory=list)
    before_run: list[str] = field(default_factory=list)
    after_run: list[str] = field(default_factory=list)
    before_remove: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkspaceHooksConfig:
        return cls(
            timeout_ms=data.get("timeout_ms", 60000),
            after_create=data.get("after_create", []),
            before_run=data.get("before_run", []),
            after_run=data.get("after_run", []),
            before_remove=data.get("before_remove", []),
        )


class WorkspaceHookError(RuntimeError):
    """Raised when a fatal workspace hook fails."""


async def _run_hook(
    name: str, command: str, cwd: Path, timeout_seconds: float
) -> tuple[int, bytes, bytes]:
    """Run a single hook command safely without shell=True."""
    # We split using shlex to avoid shell=True while still allowing basic args
    try:
        args = shlex.split(command)
    except ValueError as e:
        raise WorkspaceHookError(f"Failed to parse hook command {command!r}: {e}") from e

    if not args:
        return 0, b"", b""

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=None,  # Inherit ambient env; we could optionally sanitize
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            return proc.returncode or 0, stdout, stderr
        except asyncio.TimeoutError:
            import contextlib

            with contextlib.suppress(OSError):
                proc.kill()
            raise WorkspaceHookError(f"Hook {name!r} timed out after {timeout_seconds}s") from None
    except Exception as e:
        if isinstance(e, WorkspaceHookError):
            raise
        raise WorkspaceHookError(f"Hook {name!r} failed to execute: {e}") from e


def _truncate(b: bytes, limit: int = 1000) -> str:
    s = b.decode("utf-8", errors="replace").strip()
    if len(s) > limit:
        return s[:limit] + "... (truncated)"
    return s


async def execute_hooks(
    hook_type: str,
    cwd: Path,
    config: WorkspaceHooksConfig | None = None,
    fatal: bool = True,
) -> None:
    """Execute all commands for a specific lifecycle hook."""
    if config is None:
        return

    commands = getattr(config, hook_type, [])
    if not commands:
        return

    timeout_seconds = config.timeout_ms / 1000.0

    for cmd in commands:
        logger.info(f"Running {hook_type} hook in {cwd}: {cmd}")
        try:
            rc, _stdout, stderr = await _run_hook(hook_type, cmd, cwd, timeout_seconds)
            if rc != 0:
                err_msg = (
                    f"Hook {hook_type} command {cmd!r} exited with {rc}. "
                    f"Stderr: {_truncate(stderr)}"
                )
                if fatal:
                    raise WorkspaceHookError(err_msg)
                else:
                    logger.warning(err_msg)
        except WorkspaceHookError as e:
            if fatal:
                raise
            else:
                logger.warning(f"Hook {hook_type} failed (ignored): {e}")


def load_hooks_config(
    cwd: Path, global_config: MaxwellDaemonConfig | None = None
) -> WorkspaceHooksConfig | None:
    """Load hooks config from .maxwell/workspace_hooks.yaml or fallback to global."""
    import yaml

    local_file = cwd / ".maxwell" / "workspace_hooks.yaml"
    if local_file.exists():
        try:
            with open(local_file) as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                return WorkspaceHooksConfig.from_dict(data)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to load local workspace hooks from {local_file}: {e}")

    # Fallback to global config if available
    if global_config and hasattr(global_config, "workspace_hooks"):
        val = global_config.workspace_hooks
        if isinstance(val, dict):
            return WorkspaceHooksConfig.from_dict(val)

    return None
