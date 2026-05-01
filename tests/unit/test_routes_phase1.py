"""Tests for Phase 1 extracted route modules (issue #793).

Exercises ``maxwell_daemon.api.routes.tasks`` and
``maxwell_daemon.api.routes.control_plane`` by registering them on an
isolated FastAPI app, independent of server.py.  This keeps coverage above
the 85 % threshold introduced by the new code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from maxwell_daemon.api.routes import control_plane as cp_routes
from maxwell_daemon.api.routes import tasks as task_routes
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon


def _no_auth() -> None:
    """Dependency that always passes (no auth for test app)."""
    return None


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


@pytest.fixture
def tasks_client(daemon: Daemon) -> Iterator[TestClient]:
    """TestClient with only the tasks route module registered."""
    app = FastAPI()
    task_routes.register(app, daemon, _no_auth, _no_auth, _no_auth)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture
def cp_client(daemon: Daemon) -> Iterator[TestClient]:
    """TestClient with only the control_plane route module registered."""
    app = FastAPI()
    cp_routes.register(app, daemon, _no_auth, _no_auth, _no_auth)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# tasks.py route tests
# ---------------------------------------------------------------------------


class TestTaskRouteSubmit:
    def test_submit_task_returns_202(self, tasks_client: TestClient) -> None:
        r = tasks_client.post("/api/v1/tasks", json={"prompt": "hello phase-1"})
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "queued"
        assert body["prompt"] == "hello phase-1"

    def test_submit_task_empty_prompt_rejected(self, tasks_client: TestClient) -> None:
        r = tasks_client.post("/api/v1/tasks", json={"prompt": ""})
        assert r.status_code == 422

    def test_submit_issue_task_missing_fields_rejected(self, tasks_client: TestClient) -> None:
        r = tasks_client.post(
            "/api/v1/tasks",
            json={"prompt": "issue task", "kind": "issue"},
        )
        assert r.status_code == 422

    def test_submit_duplicate_task_id_returns_409(self, tasks_client: TestClient) -> None:
        payload = {"prompt": "dup", "task_id": "test-dup-phase1-abc"}
        r1 = tasks_client.post("/api/v1/tasks", json=payload)
        assert r1.status_code == 202
        r2 = tasks_client.post("/api/v1/tasks", json=payload)
        assert r2.status_code == 409


class TestTaskRouteList:
    def test_list_tasks_returns_200(self, tasks_client: TestClient) -> None:
        r = tasks_client.get("/api/v1/tasks")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_list_tasks_includes_submitted_task(self, tasks_client: TestClient) -> None:
        tasks_client.post("/api/v1/tasks", json={"prompt": "list me"})
        r = tasks_client.get("/api/v1/tasks")
        assert r.status_code == 200
        prompts = [t["prompt"] for t in r.json()]
        assert "list me" in prompts

    def test_list_tasks_invalid_status_returns_422(self, tasks_client: TestClient) -> None:
        r = tasks_client.get("/api/v1/tasks?status=not-a-valid-status")
        assert r.status_code == 422

    def test_legacy_api_tasks_list_returns_200(self, tasks_client: TestClient) -> None:
        r = tasks_client.get("/api/tasks?limit=10")
        assert r.status_code == 200
        body = r.json()
        assert "tasks" in body
        assert "total" in body


class TestTaskRouteGet:
    def test_get_existing_task_returns_200(self, tasks_client: TestClient) -> None:
        sub = tasks_client.post("/api/v1/tasks", json={"prompt": "get me"})
        task_id = sub.json()["id"]
        r = tasks_client.get(f"/api/v1/tasks/{task_id}")
        assert r.status_code == 200
        assert r.json()["id"] == task_id

    def test_get_missing_task_returns_404(self, tasks_client: TestClient) -> None:
        r = tasks_client.get("/api/v1/tasks/no-such-task-xyz")
        assert r.status_code == 404

    def test_legacy_api_get_task_returns_200(self, tasks_client: TestClient) -> None:
        sub = tasks_client.post("/api/v1/tasks", json={"prompt": "legacy get"})
        task_id = sub.json()["id"]
        r = tasks_client.get(f"/api/tasks/{task_id}")
        assert r.status_code == 200
        assert r.json()["id"] == task_id

    def test_legacy_api_get_missing_task_returns_404(self, tasks_client: TestClient) -> None:
        r = tasks_client.get("/api/tasks/no-such-task-xyz")
        assert r.status_code == 404


class TestTaskRouteCancel:
    def test_cancel_queued_task_returns_200(self, tasks_client: TestClient) -> None:
        sub = tasks_client.post("/api/v1/tasks", json={"prompt": "cancel me"})
        task_id = sub.json()["id"]
        r = tasks_client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    def test_cancel_missing_task_returns_404(self, tasks_client: TestClient) -> None:
        r = tasks_client.post("/api/v1/tasks/no-such-cancel-xyz/cancel")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# control_plane.py route tests
# ---------------------------------------------------------------------------


class TestControlPlaneGauntlet:
    def test_gauntlet_empty_returns_empty_list(self, cp_client: TestClient) -> None:
        r = cp_client.get("/api/v1/control-plane/gauntlet")
        assert r.status_code == 200
        assert r.json() == []

    def test_gauntlet_includes_submitted_task(self, cp_client: TestClient, daemon: Daemon) -> None:
        daemon.submit("cp gauntlet test")
        r = cp_client.get("/api/v1/control-plane/gauntlet")
        assert r.status_code == 200
        body = r.json()
        assert len(body) >= 1
        item = body[0]
        assert "task_id" in item
        assert "gates" in item
        assert "actions" in item

    def test_gauntlet_status_filter(self, cp_client: TestClient, daemon: Daemon) -> None:
        daemon.submit("filter test")
        r = cp_client.get("/api/v1/control-plane/gauntlet?status=queued")
        assert r.status_code == 200
        for item in r.json():
            assert item["status"] == "queued"

    def test_gauntlet_task_id_filter(self, cp_client: TestClient, daemon: Daemon) -> None:
        t = daemon.submit("task id filter")
        r = cp_client.get(f"/api/v1/control-plane/gauntlet?task_id={t.id}")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["task_id"] == t.id


class TestControlPlaneRetry:
    def test_retry_nonexistent_task_returns_404(self, cp_client: TestClient) -> None:
        r = cp_client.post(
            "/api/v1/control-plane/gauntlet/no-such-task/retry",
            json={"target_id": "no-such-task", "expected_status": "failed"},
        )
        assert r.status_code == 404

    def test_retry_mismatched_target_id_returns_409(
        self, cp_client: TestClient, daemon: Daemon
    ) -> None:
        t = daemon.submit("retry mismatch")
        r = cp_client.post(
            f"/api/v1/control-plane/gauntlet/{t.id}/retry",
            json={"target_id": "wrong-id", "expected_status": "failed"},
        )
        assert r.status_code == 409

    def test_retry_queued_task_wrong_expected_status_returns_409(
        self, cp_client: TestClient, daemon: Daemon
    ) -> None:
        t = daemon.submit("retry wrong status")
        r = cp_client.post(
            f"/api/v1/control-plane/gauntlet/{t.id}/retry",
            json={"target_id": t.id, "expected_status": "failed"},
        )
        assert r.status_code == 409


class TestControlPlaneCancel:
    def test_cancel_nonexistent_task_returns_404(self, cp_client: TestClient) -> None:
        r = cp_client.post(
            "/api/v1/control-plane/gauntlet/no-such-task/cancel",
            json={"target_id": "no-such-task", "expected_status": "queued"},
        )
        assert r.status_code == 404

    def test_cancel_mismatched_target_id_returns_409(
        self, cp_client: TestClient, daemon: Daemon
    ) -> None:
        t = daemon.submit("cancel mismatch")
        r = cp_client.post(
            f"/api/v1/control-plane/gauntlet/{t.id}/cancel",
            json={"target_id": "wrong-id", "expected_status": "queued"},
        )
        assert r.status_code == 409

    def test_cancel_queued_task_returns_200(self, cp_client: TestClient, daemon: Daemon) -> None:
        t = daemon.submit("cancel me cp")
        r = cp_client.post(
            f"/api/v1/control-plane/gauntlet/{t.id}/cancel",
            json={"target_id": t.id, "expected_status": "queued"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"


class TestControlPlaneWaive:
    def test_waive_nonexistent_task_returns_404(self, cp_client: TestClient) -> None:
        r = cp_client.post(
            "/api/v1/control-plane/gauntlet/no-such-task/waive",
            json={
                "target_id": "no-such-task",
                "expected_status": "failed",
                "actor": "qa",
                "reason": "known flake",
            },
        )
        assert r.status_code == 404

    def test_waive_mismatched_target_id_returns_409(
        self, cp_client: TestClient, daemon: Daemon
    ) -> None:
        t = daemon.submit("waive mismatch")
        r = cp_client.post(
            f"/api/v1/control-plane/gauntlet/{t.id}/waive",
            json={
                "target_id": "wrong-id",
                "expected_status": "failed",
                "actor": "qa",
                "reason": "known flake",
            },
        )
        assert r.status_code == 409

    def test_waive_queued_task_wrong_expected_status_returns_409(
        self, cp_client: TestClient, daemon: Daemon
    ) -> None:
        t = daemon.submit("waive wrong status")
        r = cp_client.post(
            f"/api/v1/control-plane/gauntlet/{t.id}/waive",
            json={
                "target_id": t.id,
                "expected_status": "failed",
                "actor": "qa",
                "reason": "known flake",
            },
        )
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# Model / helper unit tests (pure, no HTTP)
# ---------------------------------------------------------------------------


class TestTaskRouteModels:
    def test_task_submit_default_priority(self) -> None:
        from maxwell_daemon.api.routes.tasks import TaskSubmit

        ts = TaskSubmit(prompt="test")
        assert ts.priority == 100

    def test_task_submit_custom_priority(self) -> None:
        from maxwell_daemon.api.routes.tasks import TaskSubmit

        ts = TaskSubmit(prompt="test", priority=50)
        assert ts.priority == 50

    def test_task_submit_priority_bounds(self) -> None:
        import pydantic

        from maxwell_daemon.api.routes.tasks import TaskSubmit

        with pytest.raises(pydantic.ValidationError):
            TaskSubmit(prompt="test", priority=9999)


class TestControlPlaneHelpers:
    def test_task_is_waived_false_when_no_waiver(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _task_is_waived

        task = MagicMock()
        task.waived_by = None
        task.waiver_reason = None
        assert not _task_is_waived(task)

    def test_task_is_waived_true_when_waived(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _task_is_waived

        task = MagicMock()
        task.waived_by = "qa-bot"
        task.waiver_reason = "known flake"
        assert _task_is_waived(task)

    def test_duration_seconds_none_when_not_started(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _duration_seconds

        task = MagicMock()
        task.started_at = None
        assert _duration_seconds(task) is None

    def test_task_title_uses_issue_ref_when_present(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _task_title

        task = MagicMock()
        task.issue_repo = "org/repo"
        task.issue_number = 42
        assert _task_title(task) == "org/repo#42"

    def test_task_title_uses_prompt_when_no_issue(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _task_title

        task = MagicMock()
        task.issue_repo = None
        task.issue_number = None
        task.prompt = "a" * 100
        title = _task_title(task)
        assert len(title) <= 80

    def test_control_plane_actions_queued_returns_cancel(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _control_plane_actions_for_task

        task = MagicMock()
        task.status.value = "queued"
        task.id = "task-queued-123"
        actions = _control_plane_actions_for_task(task)
        assert len(actions) == 1
        assert actions[0].kind == "cancel"

    def test_control_plane_actions_completed_returns_empty(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _control_plane_actions_for_task

        task = MagicMock()
        task.status.value = "completed"
        task.waived_by = None
        task.waiver_reason = None
        actions = _control_plane_actions_for_task(task)
        assert actions == ()

    def test_control_plane_actions_failed_returns_retry_and_waive(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _control_plane_actions_for_task

        task = MagicMock()
        task.status.value = "failed"
        task.id = "task-failed-456"
        task.waived_by = None
        task.waiver_reason = None
        actions = _control_plane_actions_for_task(task)
        kinds = {a.kind for a in actions}
        assert "retry" in kinds
        assert "waive" in kinds

    def test_gate_statuses_for_queued_task(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _gate_statuses_for_task

        task = MagicMock()
        task.status.value = "queued"
        task.pr_url = None
        task.issue_repo = None
        task.issue_number = None
        task.prompt = "queued task"
        gates = _gate_statuses_for_task(task)
        statuses = {g.id: g.status for g in gates}
        assert statuses["intake"] == "passed"
        assert statuses["delegate"] == "pending"

    def test_gate_statuses_for_completed_task(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _gate_statuses_for_task

        task = MagicMock()
        task.status.value = "completed"
        task.pr_url = "https://github.com/org/repo/pull/1"
        task.issue_repo = None
        task.issue_number = None
        task.prompt = "done task"
        gates = _gate_statuses_for_task(task)
        statuses = {g.id: g.status for g in gates}
        assert statuses["delegate"] == "passed"
        assert statuses["verification"] == "passed"

    def test_gate_statuses_for_failed_task(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _gate_statuses_for_task

        task = MagicMock()
        task.status.value = "failed"
        task.pr_url = None
        task.issue_repo = None
        task.issue_number = None
        task.prompt = "failed task"
        task.waived_by = None
        task.waiver_reason = None
        gates = _gate_statuses_for_task(task)
        statuses = {g.id: g.status for g in gates}
        assert statuses["delegate"] == "failed"

    def test_gate_statuses_for_cancelled_task(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _gate_statuses_for_task

        task = MagicMock()
        task.status.value = "cancelled"
        task.pr_url = None
        task.issue_repo = None
        task.issue_number = None
        task.prompt = "cancelled task"
        gates = _gate_statuses_for_task(task)
        statuses = {g.id: g.status for g in gates}
        assert statuses["delegate"] == "waived"

    def test_gate_statuses_for_running_task(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _gate_statuses_for_task

        task = MagicMock()
        task.status.value = "running"
        task.pr_url = None
        task.issue_repo = None
        task.issue_number = None
        task.prompt = "running task"
        gates = _gate_statuses_for_task(task)
        statuses = {g.id: g.status for g in gates}
        assert statuses["delegate"] == "running"

    def test_critic_findings_for_failed_task(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _critic_findings_for_task

        task = MagicMock()
        task.status.value = "failed"
        task.error = "timeout"
        task.result = None
        findings = _critic_findings_for_task(task)
        assert len(findings) >= 1
        assert findings[0].severity == "blocker"

    def test_critic_findings_for_queued_task(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.routes.control_plane import _critic_findings_for_task

        task = MagicMock()
        task.status.value = "queued"
        task.error = None
        task.result = None
        findings = _critic_findings_for_task(task)
        assert len(findings) >= 1
        assert findings[0].severity == "note"
