"""A/B dispatch — race two backends on the same issue, pick the winner."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import TaskKind


@pytest.fixture
def dual_config(register_recording_backend: None) -> MaxwellDaemonConfig:
    return MaxwellDaemonConfig.model_validate(
        {
            "backends": {
                "primary": {"type": "recording", "model": "m-primary"},
                "local": {"type": "recording", "model": "m-local"},
            },
            "agent": {"default_backend": "primary"},
        }
    )


@pytest.fixture
def client(
    dual_config: MaxwellDaemonConfig, isolated_ledger_path: Path, tmp_path: Path
) -> Iterator[tuple[TestClient, Daemon]]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(
        dual_config,
        ledger_path=isolated_ledger_path,
        task_store_path=tmp_path / "tasks.db",
    )
    try:
        with TestClient(create_app(d)) as c:
            yield c, d
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class TestAbEndpoint:
    def test_creates_two_tasks_with_different_backends(
        self, client: tuple[TestClient, Daemon]
    ) -> None:
        c, daemon = client
        r = c.post(
            "/api/v1/issues/ab-dispatch",
            json={
                "repo": "owner/r",
                "number": 42,
                "backends": ["primary", "local"],
                "mode": "plan",
            },
        )
        assert r.status_code == 202
        body = r.json()
        assert len(body["tasks"]) == 2
        assert {t["backend"] for t in body["tasks"]} == {"primary", "local"}

        # Both must exist in the daemon's state, both as ISSUE kind.
        state = daemon.state()
        issue_tasks = [t for t in state.tasks.values() if t.kind is TaskKind.ISSUE]
        assert len(issue_tasks) == 2
        # Tasks must carry an ab_group so the UI can pair them.
        ab_groups = {t.ab_group for t in issue_tasks if t.ab_group}
        assert len(ab_groups) == 1

    def test_requires_at_least_two_backends(self, client: tuple[TestClient, Daemon]) -> None:
        c, _ = client
        r = c.post(
            "/api/v1/issues/ab-dispatch",
            json={"repo": "owner/r", "number": 1, "backends": ["primary"]},
        )
        assert r.status_code == 422

    def test_rejects_duplicate_backends(self, client: tuple[TestClient, Daemon]) -> None:
        c, _ = client
        r = c.post(
            "/api/v1/issues/ab-dispatch",
            json={
                "repo": "owner/r",
                "number": 1,
                "backends": ["primary", "primary"],
            },
        )
        assert r.status_code == 422

    def test_unknown_backend_rejected(self, client: tuple[TestClient, Daemon]) -> None:
        c, _ = client
        r = c.post(
            "/api/v1/issues/ab-dispatch",
            json={
                "repo": "owner/r",
                "number": 1,
                "backends": ["primary", "nonexistent"],
            },
        )
        # The daemon rejects the second submission; we still dispatched the
        # first, but the endpoint reports the partial failure.
        assert r.status_code in {207, 400, 422}


class TestDaemonAbSubmit:
    def test_submit_ab_sets_group(
        self,
        dual_config: MaxwellDaemonConfig,
        isolated_ledger_path: Path,
        tmp_path: Path,
    ) -> None:
        d = Daemon(
            dual_config,
            ledger_path=isolated_ledger_path,
            task_store_path=tmp_path / "t.db",
        )
        tasks = d.submit_issue_ab(
            repo="owner/r",
            issue_number=7,
            backends=["primary", "local"],
            mode="plan",
        )
        assert len(tasks) == 2
        assert tasks[0].ab_group == tasks[1].ab_group
        assert tasks[0].backend != tasks[1].backend
