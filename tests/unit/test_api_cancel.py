"""POST /api/v1/tasks/{id}/cancel — cancel queued tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon


@pytest.fixture
def client(
    minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
) -> Iterator[tuple[TestClient, Daemon]]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
    # Do NOT start workers — we want tasks to stay queued.
    try:
        with TestClient(create_app(d)) as c:
            yield c, d
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class TestCancel:
    def test_cancel_queued_task(self, client: tuple[TestClient, Daemon]) -> None:
        c, daemon = client
        task = daemon.submit("hello")
        r = c.post(f"/api/v1/tasks/{task.id}/cancel")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "cancelled"
        # Daemon's view should match.
        assert daemon.get_task(task.id).status.value == "cancelled"  # type: ignore[union-attr]

    def test_cancel_missing_returns_404(
        self, client: tuple[TestClient, Daemon]
    ) -> None:
        c, _ = client
        r = c.post("/api/v1/tasks/nonexistent/cancel")
        assert r.status_code == 404

    def test_cancel_already_completed_returns_409(
        self, client: tuple[TestClient, Daemon]
    ) -> None:
        from maxwell_daemon.daemon.runner import TaskStatus

        c, daemon = client
        task = daemon.submit("hi")
        # Simulate it having finished.
        daemon.get_task(task.id).status = TaskStatus.COMPLETED  # type: ignore[union-attr]
        r = c.post(f"/api/v1/tasks/{task.id}/cancel")
        assert r.status_code == 409
