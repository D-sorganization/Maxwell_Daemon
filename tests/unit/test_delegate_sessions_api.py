"""API tests for durable delegate session readback."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.core.delegate_lifecycle import (
    DelegateSession,
    DelegateSessionStatus,
    LeaseRecoveryPolicy,
)
from maxwell_daemon.daemon import Daemon


def _session(
    *,
    session_id: str = "session-1",
    status: DelegateSessionStatus = DelegateSessionStatus.QUEUED,
) -> DelegateSession:
    created_at = datetime(2026, 4, 22, 11, 0, tzinfo=timezone.utc)
    return DelegateSession(
        id=session_id,
        delegate_id="delegate-1",
        work_item_id="issue-395",
        workspace_ref="worktree://issue-395",
        backend_ref="codex-cli",
        machine_ref="worker-a",
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )


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
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def client(daemon: Daemon) -> Iterator[TestClient]:
    with TestClient(create_app(daemon)) as c:
        yield c


def test_list_and_show_delegate_sessions(client: TestClient, daemon: Daemon) -> None:
    service = daemon.delegate_lifecycle
    service.create_session(_session())
    service.acquire_lease(
        "session-1",
        owner_id="worker-a",
        ttl=timedelta(minutes=5),
        recovery_policy=LeaseRecoveryPolicy.RECOVERABLE,
    )
    service.mark_running("session-1", owner_id="worker-a")
    service.record_checkpoint(
        "session-1",
        current_plan="Keep a durable checkpoint for recovery.",
        changed_files=("maxwell_daemon/core/delegate_lifecycle.py",),
        test_commands=("pytest tests/unit/test_delegate_sessions_api.py",),
        failures_and_learnings=("The API should expose the latest checkpoint.",),
        artifact_refs=("artifact://patch-3",),
        resume_prompt="Resume from the last checkpoint.",
    )

    listed = client.get(
        "/api/v1/delegate-sessions", params={"work_item_id": "issue-395"}
    )
    assert listed.status_code == 200
    body = listed.json()
    assert len(body) == 1
    assert body[0]["session"]["id"] == "session-1"
    assert body[0]["session"]["status"] == "running"
    assert body[0]["latest_checkpoint"]["current_plan"].startswith(
        "Keep a durable checkpoint"
    )

    fetched = client.get("/api/v1/delegate-sessions/session-1")
    assert fetched.status_code == 200
    snapshot = fetched.json()
    assert snapshot["session"]["work_item_id"] == "issue-395"
    assert snapshot["active_lease"]["owner_id"] == "worker-a"
    assert (
        snapshot["latest_checkpoint"]["resume_prompt"]
        == "Resume from the last checkpoint."
    )
