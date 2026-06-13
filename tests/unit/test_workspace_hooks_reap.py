"""workspace_hooks._run_hook must reap timed-out children (no zombies) — #980."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from maxwell_daemon.daemon import workspace_hooks
from maxwell_daemon.daemon.workspace_hooks import WorkspaceHookError, _run_hook


class _HungProc:
    """A subprocess that never finishes communicate() until killed."""

    returncode = None

    def __init__(self) -> None:
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(10)  # exceeds the test's tiny timeout
        return b"", b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return -9


def test_timed_out_hook_is_killed_and_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _HungProc()

    async def fake_create(*args: object, **kwargs: object) -> _HungProc:
        return proc

    monkeypatch.setattr(workspace_hooks.asyncio, "create_subprocess_exec", fake_create)

    async def _run() -> None:
        with pytest.raises(WorkspaceHookError, match="timed out"):
            await _run_hook("slow", "sleep 100", tmp_path, timeout_seconds=0.05)

    asyncio.run(_run())

    assert proc.killed is True
    # The killed child must be reaped via proc.wait() so no zombie lingers.
    assert proc.waited is True
