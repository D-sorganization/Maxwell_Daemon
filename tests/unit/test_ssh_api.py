"""SSH API endpoint contract tests."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon


@pytest.fixture
def daemon(
    minimal_config: MaxwellDaemonConfig,
    isolated_ledger_path: Path,
    tmp_path: Path,
) -> Iterator[Daemon]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(
        minimal_config,
        ledger_path=isolated_ledger_path,
        task_store_path=tmp_path / "tasks.db",
        work_item_store_path=tmp_path / "work_items.db",
        task_graph_store_path=tmp_path / "task_graphs.db",
        artifact_store_path=tmp_path / "artifacts.db",
        artifact_blob_root=tmp_path / "artifacts",
        action_store_path=tmp_path / "actions.db",
        delegate_lifecycle_store_path=tmp_path / "delegate_sessions.db",
    )
    loop.run_until_complete(d.start(worker_count=1))
    try:
        yield d
    finally:
        loop.run_until_complete(d.stop())
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


class TestSSHEndpointsWithoutAuth:
    def test_ssh_sessions_still_fail_closed_when_auth_unconfigured(
        self, daemon: Daemon, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class InstalledPool:
            def sessions(self) -> list[dict[str, Any]]:
                return []

        from maxwell_daemon.ssh import session as ssh_session

        monkeypatch.setitem(sys.modules, "asyncssh", object())
        monkeypatch.setattr(ssh_session, "SSHSessionPool", InstalledPool)
        with TestClient(create_app(daemon)) as c:
            r = c.get("/api/v1/ssh/sessions")

        assert r.status_code == 503
        assert "SSH endpoints are disabled" in r.json()["detail"]
