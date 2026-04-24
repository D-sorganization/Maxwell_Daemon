"""FastAPI REST server — endpoint contracts, auth, and error paths."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.api import server as api_server
from maxwell_daemon.backends import (
    BackendCapabilities,
    BackendRegistry,
    BackendResponse,
    ILLMBackend,
    Message,
    TokenUsage,
)
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.core.actions import ActionKind
from maxwell_daemon.core.artifacts import ArtifactKind
from maxwell_daemon.core.delegate_lifecycle import DelegateSession, LeaseRecoveryPolicy
from maxwell_daemon.core.work_items import AcceptanceCriterion, ScopeBoundary, WorkItem
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import Task, TaskKind, TaskStatus
from maxwell_daemon.director import GraphExecutionContext, GraphNodeOutput
from maxwell_daemon.fleet.capabilities import (
    FleetNode,
    NodeCapability,
    NodePolicy,
    NodeResourceSnapshot,
    TailscalePeerStatus,
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


@pytest.fixture
def auth_client(daemon: Daemon) -> Iterator[TestClient]:
    with TestClient(create_app(daemon, auth_token="secret-abc")) as c:  # nosec B106 — intentional test fixture, not a real credential
        yield c


class TestHealth:
    def test_returns_ok(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "uptime_seconds" in body
        assert "version" in body

    def test_health_does_not_require_auth(self, auth_client: TestClient) -> None:
        r = auth_client.get("/health")
        assert r.status_code == 200

    def test_readyz_reports_ready_when_backend_available(self, client: TestClient) -> None:
        r = client.get("/readyz")

        assert r.status_code == 200
        assert r.json() == {"status": "ready"}

    def test_readyz_reports_unavailable_without_backends(
        self,
        client: TestClient,
        daemon: Daemon,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(daemon, "state", lambda: SimpleNamespace(backends_available=[]))

        r = client.get("/readyz")

        assert r.status_code == 503
        assert r.json()["detail"] == "no backends available"


class TestBackends:
    def test_list_returns_configured_backends(self, client: TestClient) -> None:
        r = client.get("/api/v1/backends")
        assert r.status_code == 200
        assert "primary" in r.json()["backends"]

    def test_available_returns_backend_catalog_for_onboarding(
        self,
        client: TestClient,
        daemon: Daemon,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class CatalogBackend(ILLMBackend):
            async def complete(
                self,
                messages: list[Message],
                *,
                model: str,
                **_: Any,
            ) -> BackendResponse:
                return BackendResponse(
                    content="ok",
                    finish_reason="stop",
                    usage=TokenUsage(total_tokens=1),
                    model=model,
                    backend="catalog-test",
                )

            async def stream(
                self,
                messages: list[Message],
                *,
                model: str,
                **_: Any,
            ) -> AsyncIterator[str]:
                if False:
                    yield model

            async def health_check(self) -> bool:
                return True

            def capabilities(self, model: str) -> BackendCapabilities:
                return BackendCapabilities()

        daemon._config = MaxwellDaemonConfig.model_validate(
            {
                "backends": {
                    "cloud": {"type": "claude", "model": "claude-sonnet-4-6"},
                    "local": {"type": "ollama", "model": "llama3.2"},
                },
                "agent": {"default_backend": "cloud"},
            }
        )
        fake_registry = BackendRegistry()
        fake_registry.register("claude", CatalogBackend)
        fake_registry.register("ollama", CatalogBackend)
        monkeypatch.setattr(api_server, "registry", fake_registry)
        monkeypatch.setattr(daemon, "state", lambda: SimpleNamespace(backends_available=["cloud"]))

        response = client.get("/api/v1/backends/available")

        assert response.status_code == 200
        catalog = {item["name"]: item for item in response.json()["backends"]}
        assert catalog["claude"]["configured_aliases"] == ["cloud"]
        assert catalog["claude"]["loaded"] is True
        assert catalog["claude"]["connected"] is True
        assert catalog["claude"]["api_key_env_var"] == "ANTHROPIC_API_KEY"
        assert catalog["ollama"]["configured_aliases"] == ["local"]
        assert catalog["ollama"]["loaded"] is True
        assert catalog["ollama"]["connected"] is False
        assert catalog["ollama"]["default_endpoint"] == "http://localhost:11434"
        assert catalog["openai"]["configured_aliases"] == []
        assert catalog["openai"]["loaded"] is False
        assert catalog["openai"]["connected"] is False


class TestTaskSubmission:
    def test_submit_returns_202_and_queued_task(self, client: TestClient) -> None:
        r = client.post("/api/v1/tasks", json={"prompt": "hi"})
        assert r.status_code == 202
        body = r.json()
        assert body["prompt"] == "hi"
        assert body["status"] in {"queued", "running", "completed"}
        assert body["id"]

    def test_submit_rejects_empty_prompt(self, client: TestClient) -> None:
        r = client.post("/api/v1/tasks", json={"prompt": ""})
        assert r.status_code == 422

    def test_submit_duplicate_task_id_returns_conflict(
        self, client: TestClient, daemon: Daemon
    ) -> None:
        first = client.post(
            "/api/v1/tasks",
            json={"prompt": "first", "task_id": "api-duplicate-id"},
        )
        assert first.status_code == 202

        duplicate = client.post(
            "/api/v1/tasks",
            json={"prompt": "second", "task_id": "api-duplicate-id"},
        )

        assert duplicate.status_code == 409
        assert "api-duplicate-id" in duplicate.json()["detail"]
        task = daemon._task_store.get("api-duplicate-id")
        assert task is not None
        assert task.prompt == "first"

    def test_submit_rejects_invalid_issue_mode(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/tasks",
            json={
                "prompt": "owner/repo#123",
                "kind": "issue",
                "issue_repo": "owner/repo",
                "issue_number": 123,
                "issue_mode": "invalid",
            },
        )

        assert r.status_code == 422

    @pytest.mark.parametrize(
        "payload",
        [
            {"prompt": "owner/repo#123", "kind": "issue", "issue_number": 123},
            {"prompt": "owner/repo#123", "kind": "issue", "issue_repo": "owner/repo"},
        ],
    )
    def test_submit_rejects_incomplete_issue_payload(
        self, client: TestClient, payload: dict[str, object]
    ) -> None:
        r = client.post("/api/v1/tasks", json=payload)

        assert r.status_code == 422
        assert "issue_repo and issue_number" in r.json()["detail"]

    def test_submit_maps_issue_submission_value_error_to_422(
        self,
        client: TestClient,
        daemon: Daemon,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def raise_value_error(**_: object) -> Task:
            raise ValueError("unsupported issue mode")

        monkeypatch.setattr(daemon, "submit_issue", raise_value_error)

        r = client.post(
            "/api/v1/tasks",
            json={
                "prompt": "owner/repo#123",
                "kind": "issue",
                "issue_repo": "owner/repo",
                "issue_number": 123,
            },
        )

        assert r.status_code == 422
        assert r.json()["detail"] == "unsupported issue mode"

    def test_list_returns_all_tasks(self, client: TestClient) -> None:
        for i in range(3):
            client.post("/api/v1/tasks", json={"prompt": f"t{i}"})
        r = client.get("/api/v1/tasks")
        assert r.status_code == 200
        assert len(r.json()) >= 3

    def test_list_filters_by_status_kind_and_repo(self, client: TestClient, daemon: Daemon) -> None:
        matching = Task(
            id="match",
            prompt="owner/repo#7",
            kind=TaskKind.ISSUE,
            repo="owner/repo",
            issue_repo="owner/repo",
            issue_number=7,
            status=TaskStatus.RUNNING,
        )
        other = Task(
            id="other",
            prompt="other/repo#9",
            kind=TaskKind.ISSUE,
            repo="other/repo",
            issue_repo="other/repo",
            issue_number=9,
            status=TaskStatus.QUEUED,
        )
        with daemon._tasks_lock:
            daemon._tasks[matching.id] = matching
            daemon._tasks[other.id] = other

        r = client.get("/api/v1/tasks?status=running&kind=issue&repo=owner/repo&limit=10")

        assert r.status_code == 200
        body = r.json()
        assert [task["id"] for task in body] == ["match"]

    def test_list_filters_by_completed_before(self, client: TestClient, daemon: Daemon) -> None:
        old_done = Task(
            id="old-done",
            prompt="old",
            status=TaskStatus.COMPLETED,
            finished_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        recent_done = Task(
            id="recent-done",
            prompt="recent",
            status=TaskStatus.COMPLETED,
            finished_at=datetime.now(timezone.utc),
        )
        with daemon._tasks_lock:
            daemon._tasks[old_done.id] = old_done
            daemon._tasks[recent_done.id] = recent_done

        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        r = client.get("/api/v1/tasks", params={"completed_before": cutoff})

        assert r.status_code == 200
        assert [task["id"] for task in r.json()] == ["old-done"]

    @pytest.mark.parametrize("query_name", ["completed_before", "completedBefore"])
    def test_list_filters_with_naive_completed_before(
        self,
        client: TestClient,
        daemon: Daemon,
        query_name: str,
    ) -> None:
        old_done = Task(
            id=f"old-done-{query_name}",
            prompt="old",
            status=TaskStatus.COMPLETED,
            finished_at=datetime(2026, 4, 19, tzinfo=timezone.utc),
        )
        recent_done = Task(
            id=f"recent-done-{query_name}",
            prompt="recent",
            status=TaskStatus.COMPLETED,
            finished_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
        )
        with daemon._tasks_lock:
            daemon._tasks[old_done.id] = old_done
            daemon._tasks[recent_done.id] = recent_done

        r = client.get("/api/v1/tasks", params={query_name: "2026-04-20T00:00:00"})

        assert r.status_code == 200
        assert [task["id"] for task in r.json()] == [old_done.id]

    def test_get_task_by_id(self, client: TestClient) -> None:
        submitted = client.post("/api/v1/tasks", json={"prompt": "x"}).json()
        r = client.get(f"/api/v1/tasks/{submitted['id']}")
        assert r.status_code == 200
        assert r.json()["id"] == submitted["id"]

    def test_get_task_includes_dispatched_worker_metadata(
        self, client: TestClient, daemon: Daemon
    ) -> None:
        task = Task(
            id="task-dispatched",
            prompt="owner/repo#9",
            kind=TaskKind.ISSUE,
            repo="owner/repo",
            issue_repo="owner/repo",
            issue_number=9,
            status=TaskStatus.DISPATCHED,
            dispatched_to="worker-west",
        )
        with daemon._tasks_lock:
            daemon._tasks[task.id] = task

        r = client.get(f"/api/v1/tasks/{task.id}")

        assert r.status_code == 200
        assert r.json()["status"] == "dispatched"
        assert r.json()["dispatched_to"] == "worker-west"

    def test_get_nonexistent_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/v1/tasks/nonexistent-id")
        assert r.status_code == 404


class TestControlPlaneGauntlet:
    def test_returns_ordered_gate_timeline_and_delegate_status(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        task = Task(
            id="issue-7",
            prompt="owner/repo#7",
            kind=TaskKind.ISSUE,
            repo="owner/repo",
            issue_repo="owner/repo",
            issue_number=7,
            status=TaskStatus.RUNNING,
            backend="primary",
            model="gpt-4.1",
            route_reason="repo override for owner/repo",
            started_at=datetime.now(timezone.utc),
        )
        with daemon._tasks_lock:
            daemon._tasks[task.id] = task

        r = client.get("/api/v1/control-plane/gauntlet")

        assert r.status_code == 200
        item = next(row for row in r.json() if row["task_id"] == "issue-7")
        assert item["title"] == "owner/repo#7"
        assert item["current_gate"] == "Delegate session"
        assert [gate["id"] for gate in item["gates"]] == ["intake", "delegate", "verification"]
        assert [gate["status"] for gate in item["gates"]] == ["passed", "running", "blocked"]
        assert item["delegates"][0]["status"] == "running"
        assert item["resource_routing"]["selected_backend"] == "primary"
        assert item["resource_routing"]["selected_model"] == "gpt-4.1"
        assert item["resource_routing"]["selection_reason"] == "repo override for owner/repo"

    def test_failed_task_surfaces_blocker_before_notes(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        failed = Task(
            id="failed",
            prompt="broken task",
            status=TaskStatus.FAILED,
            error="unit tests failed",
            result="pytest output",
        )
        queued = Task(id="queued", prompt="queued task", status=TaskStatus.QUEUED)
        with daemon._tasks_lock:
            daemon._tasks[failed.id] = failed
            daemon._tasks[queued.id] = queued

        r = client.get("/api/v1/control-plane/gauntlet?limit=10")

        assert r.status_code == 200
        by_id = {row["task_id"]: row for row in r.json()}
        failed_item = by_id["failed"]
        assert failed_item["final_decision"] == "fail"
        assert (
            failed_item["next_action"]
            == "Inspect blocker evidence, then retry or waive with a reason"
        )
        assert failed_item["critic_findings"][0]["severity"] == "blocker"
        failed_gate = failed_item["gates"][1]
        assert failed_gate["retry_allowed"] is True
        assert failed_gate["waiver_allowed"] is True
        assert by_id["queued"]["critic_findings"][0]["severity"] == "note"

    def test_uses_delegate_session_snapshots_when_available(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        task = Task(
            id="task-with-session",
            prompt="owner/repo#91",
            kind=TaskKind.ISSUE,
            repo="owner/repo",
            issue_repo="owner/repo",
            issue_number=91,
            status=TaskStatus.RUNNING,
            backend="primary",
            started_at=datetime.now(timezone.utc),
        )
        with daemon._tasks_lock:
            daemon._tasks[task.id] = task

        work_item = daemon.create_work_item(
            WorkItem(id="wi-91", title="Ship the gate dashboard slice")
        )
        service = daemon.delegate_lifecycle
        service.create_session(
            DelegateSession(
                id="session-91",
                delegate_id="implementer",
                work_item_id=work_item.id,
                task_id=task.id,
                workspace_ref="worktree://task-with-session",
                backend_ref="codex-cli",
                machine_ref="worker-a",
            )
        )
        service.acquire_lease(
            "session-91",
            owner_id="worker-a",
            ttl=timedelta(minutes=5),
            recovery_policy=LeaseRecoveryPolicy.RECOVERABLE,
        )
        service.mark_running("session-91", owner_id="worker-a")
        service.record_checkpoint(
            "session-91",
            current_plan="Render the current gate and real delegate checkpoint.",
            failures_and_learnings=("Keep blocker detail visible near the action controls.",),
        )

        r = client.get("/api/v1/control-plane/gauntlet", params={"task_id": task.id})

        assert r.status_code == 200
        item = r.json()[0]
        assert item["work_item_id"] == "wi-91"
        assert item["work_item_status"] == work_item.status.value
        assert item["delegates"][0]["id"] == "session-91"
        assert item["delegates"][0]["status"] == "running"
        assert item["delegates"][0]["backend"] == "codex-cli"
        assert item["delegates"][0]["machine"] == "worker-a"
        assert item["delegates"][0]["role"] == "implementer"
        assert item["delegates"][0]["latest_checkpoint"].startswith(
            "Render the current gate and real delegate checkpoint."
        )

    def test_filters_by_task_id(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        matching = Task(id="task-match", prompt="keep me", status=TaskStatus.RUNNING)
        other = Task(id="task-other", prompt="skip me", status=TaskStatus.FAILED)
        with daemon._tasks_lock:
            daemon._tasks[matching.id] = matching
            daemon._tasks[other.id] = other

        r = client.get("/api/v1/control-plane/gauntlet", params={"task_id": "task-match"})

        assert r.status_code == 200
        assert [row["task_id"] for row in r.json()] == ["task-match"]

    def test_filters_by_status(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        failed = Task(id="failed-only", prompt="broken", status=TaskStatus.FAILED)
        queued = Task(id="queued-only", prompt="waiting", status=TaskStatus.QUEUED)
        with daemon._tasks_lock:
            daemon._tasks[failed.id] = failed
            daemon._tasks[queued.id] = queued

        r = client.get("/api/v1/control-plane/gauntlet", params={"status": "failed"})

        assert r.status_code == 200
        assert [row["task_id"] for row in r.json()] == ["failed-only"]

    def test_cancelled_task_surfaces_cancelled_decision_and_delegate_waived(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        cancelled = Task(
            id="cancelled",
            prompt="cancelled task",
            status=TaskStatus.CANCELLED,
        )
        with daemon._tasks_lock:
            daemon._tasks[cancelled.id] = cancelled

        r = client.get("/api/v1/control-plane/gauntlet")

        assert r.status_code == 200
        item = next(row for row in r.json() if row["task_id"] == "cancelled")
        assert item["final_decision"] == "cancelled"
        assert item["next_action"] == "No action required unless the task should be requeued"
        assert item["gates"][1]["status"] == "waived"
        assert "cancelled by policy or operator action" in item["gates"][1]["next_action"]
        assert item["gates"][2]["status"] == "blocked"

    def test_unknown_status_falls_back_to_blocked_gate_and_default_next_action(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        unknown = Task(id="unknown-status", prompt="unknown status task")
        unknown.status = SimpleNamespace(value="mystery")  # type: ignore[assignment]
        with daemon._tasks_lock:
            daemon._tasks[unknown.id] = unknown

        r = client.get("/api/v1/control-plane/gauntlet")

        assert r.status_code == 200
        item = next(row for row in r.json() if row["task_id"] == "unknown-status")
        assert item["status"] == "mystery"
        assert item["final_decision"] == "blocked"
        assert item["next_action"] == "Inspect task state"
        assert item["gates"][1]["status"] == "blocked"
        assert item["gates"][1]["next_action"] == "Unknown task status 'mystery'"


class TestControlPlaneActions:
    def test_queued_task_exposes_cancel_action(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        task = Task(
            id="cancelable",
            prompt="queued task",
            status=TaskStatus.QUEUED,
        )
        with daemon._tasks_lock:
            daemon._tasks[task.id] = task

        r = client.get("/api/v1/control-plane/gauntlet")

        assert r.status_code == 200
        item = next(row for row in r.json() if row["task_id"] == "cancelable")
        assert [action["kind"] for action in item["actions"]] == ["cancel"]
        assert item["actions"][0]["path"].endswith("/cancel")
        assert item["actions"][0]["expected_status"] == "queued"

    def test_failed_task_exposes_retry_and_waive_actions(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        task = Task(
            id="actionable",
            prompt="broken task",
            status=TaskStatus.FAILED,
            error="unit tests failed",
        )
        with daemon._tasks_lock:
            daemon._tasks[task.id] = task

        r = client.get("/api/v1/control-plane/gauntlet")

        assert r.status_code == 200
        item = next(row for row in r.json() if row["task_id"] == "actionable")
        assert [action["kind"] for action in item["actions"]] == ["retry", "waive"]
        assert item["actions"][0]["target_id"] == "actionable"
        assert item["actions"][1]["requires_reason"] is True
        assert item["actions"][1]["requires_actor"] is True

    def test_non_failed_task_hides_actions(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        task = Task(
            id="completed",
            prompt="done",
            status=TaskStatus.COMPLETED,
            result="ok",
        )
        with daemon._tasks_lock:
            daemon._tasks[task.id] = task

        r = client.get("/api/v1/control-plane/gauntlet")

        assert r.status_code == 200
        item = next(row for row in r.json() if row["task_id"] == "completed")
        assert item["actions"] == []

    def test_retry_requeues_task_and_clears_failure_metadata(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        task = Task(
            id="retry-me",
            prompt="broken task",
            status=TaskStatus.FAILED,
            error="unit tests failed",
            result="traceback",
        )
        with daemon._tasks_lock:
            daemon._tasks[task.id] = task
        daemon._task_store.save(task)

        r = client.post(
            "/api/v1/control-plane/gauntlet/retry-me/retry",
            json={"target_id": "retry-me", "expected_status": "failed"},
        )

        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "queued"
        assert [action["kind"] for action in body["actions"]] == ["cancel"]
        retried = daemon.get_task("retry-me")
        assert retried is not None
        assert retried.status is TaskStatus.QUEUED
        assert retried.error is None
        assert retried.result is None

    def test_cancel_marks_queued_task_cancelled(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        task = Task(
            id="cancel-me",
            prompt="queued task",
            status=TaskStatus.QUEUED,
        )
        with daemon._tasks_lock:
            daemon._tasks[task.id] = task
        daemon._task_store.save(task)

        r = client.post(
            "/api/v1/control-plane/gauntlet/cancel-me/cancel",
            json={"target_id": "cancel-me", "expected_status": "queued"},
        )

        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "cancelled"
        assert body["final_decision"] == "cancelled"
        assert body["actions"] == []
        cancelled = daemon.get_task("cancel-me")
        assert cancelled is not None
        assert cancelled.status is TaskStatus.CANCELLED

    def test_cancel_rejects_stale_expected_state(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        task = Task(
            id="stale-cancel",
            prompt="running task",
            status=TaskStatus.RUNNING,
        )
        with daemon._tasks_lock:
            daemon._tasks[task.id] = task

        r = client.post(
            "/api/v1/control-plane/gauntlet/stale-cancel/cancel",
            json={"target_id": "stale-cancel", "expected_status": "queued"},
        )

        assert r.status_code == 409
        assert "expected queued" in r.json()["detail"]

    def test_retry_rejects_stale_expected_state(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        task = Task(
            id="stale-retry",
            prompt="queued task",
            status=TaskStatus.QUEUED,
        )
        with daemon._tasks_lock:
            daemon._tasks[task.id] = task

        r = client.post(
            "/api/v1/control-plane/gauntlet/stale-retry/retry",
            json={"target_id": "stale-retry", "expected_status": "failed"},
        )

        assert r.status_code == 409
        assert "expected failed" in r.json()["detail"]

    def test_waive_requires_actor_and_reason(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        task = Task(
            id="needs-waiver",
            prompt="broken task",
            status=TaskStatus.FAILED,
            error="unit tests failed",
        )
        with daemon._tasks_lock:
            daemon._tasks[task.id] = task

        r = client.post(
            "/api/v1/control-plane/gauntlet/needs-waiver/waive",
            json={
                "target_id": "needs-waiver",
                "expected_status": "failed",
                "reason": "accepted risk",
            },
        )

        assert r.status_code == 422

    def test_waive_records_actor_and_reason_without_marking_passed(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        task = Task(
            id="waive-me",
            prompt="broken task",
            status=TaskStatus.FAILED,
            error="unit tests failed",
        )
        with daemon._tasks_lock:
            daemon._tasks[task.id] = task

        r = client.post(
            "/api/v1/control-plane/gauntlet/waive-me/waive",
            json={
                "target_id": "waive-me",
                "expected_status": "failed",
                "actor": "reviewer",
                "reason": "temporary exception",
            },
        )

        assert r.status_code == 200
        body = r.json()
        assert body["final_decision"] == "waived"
        assert body["actions"] == []
        assert body["gates"][1]["status"] == "waived"
        assert "reviewer" in body["next_action"]
        waived = daemon.get_task("waive-me")
        assert waived is not None
        assert waived.status is TaskStatus.FAILED
        assert waived.waived_by == "reviewer"
        assert waived.waiver_reason == "temporary exception"


class TestCostEndpoint:
    def test_cost_summary_structure(self, client: TestClient) -> None:
        r = client.get("/api/v1/cost")
        assert r.status_code == 200
        body = r.json()
        assert "month_to_date_usd" in body
        assert "by_backend" in body
        assert body["month_to_date_usd"] >= 0.0


class TestAdminPruneEndpoint:
    def test_prune_endpoint_runs_retention(self, client: TestClient, daemon: Daemon) -> None:
        old_done = Task(
            id="old-prune",
            prompt="old",
            status=TaskStatus.COMPLETED,
            finished_at=datetime.now(timezone.utc) - timedelta(days=40),
        )
        with daemon._tasks_lock:
            daemon._tasks[old_done.id] = old_done
        daemon._task_store.save(old_done)

        r = client.get("/api/v1/admin/prune?older_than_days=30")

        assert r.status_code == 200
        assert r.json()["tasks_pruned"] == 1
        assert daemon.get_task(old_done.id) is None


class _FakeGraphExecutor:
    def __init__(self) -> None:
        self.contexts: list[GraphExecutionContext] = []

    def execute(self, context: GraphExecutionContext) -> GraphNodeOutput:
        self.contexts.append(context)
        return GraphNodeOutput(text=f"{context.node.id} output")


class TestTaskGraphEndpoints:
    def test_create_list_and_get_task_graph(self, client: TestClient, daemon: Daemon) -> None:
        daemon.create_work_item(
            WorkItem(
                id="wi-graph",
                title="Graph work",
                acceptance_criteria=(
                    AcceptanceCriterion(id="ac-1", text="plan exists"),
                    AcceptanceCriterion(id="ac-2", text="tests pass"),
                    AcceptanceCriterion(id="ac-3", text="review passes"),
                ),
                scope=ScopeBoundary(risk_level="medium"),
            )
        )

        created = client.post(
            "/api/v1/task-graphs",
            json={
                "id": "graph-api",
                "work_item_id": "wi-graph",
                "template": "standard-delivery",
            },
        )
        listed = client.get("/api/v1/task-graphs?work_item_id=wi-graph")
        detail = client.get("/api/v1/task-graphs/graph-api")

        assert created.status_code == 201
        assert created.json()["graph"]["id"] == "graph-api"
        assert created.json()["graph"]["status"] == "queued"
        assert created.json()["node_runs"] == []
        assert [item["graph"]["id"] for item in listed.json()] == ["graph-api"]
        assert detail.status_code == 200
        assert [node["id"] for node in detail.json()["graph"]["nodes"]] == [
            "planner",
            "implementer",
            "qa",
            "reviewer",
        ]

    def test_create_task_graph_rejects_missing_work_item(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/task-graphs",
            json={"work_item_id": "missing"},
        )

        assert response.status_code == 404

    def test_start_reports_unavailable_without_executor(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        daemon.create_work_item(WorkItem(id="wi-graph", title="Graph work"))
        graph = daemon.create_task_graph(
            "wi-graph",
            graph_id="graph-unavailable",
        )

        response = client.post(f"/api/v1/task-graphs/{graph.graph.id}/start")

        assert response.status_code == 503
        assert "executor is not configured" in response.json()["detail"]

    def test_start_runs_graph_and_persists_node_runs(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        daemon.create_work_item(WorkItem(id="wi-graph", title="Graph work"))
        graph = daemon.create_task_graph(
            "wi-graph",
            graph_id="graph-run",
            template=None,
        )
        executor = _FakeGraphExecutor()
        daemon.set_task_graph_executor(executor)

        response = client.post(f"/api/v1/task-graphs/{graph.graph.id}/start")
        detail = client.get(f"/api/v1/task-graphs/{graph.graph.id}")

        assert response.status_code == 200
        assert response.json()["graph"]["status"] == "completed"
        assert [run["status"] for run in response.json()["node_runs"]] == [
            "completed",
            "completed",
            "completed",
            "completed",
        ]
        assert len(executor.contexts) == 4
        assert detail.json()["node_runs"][0]["artifact_ids"]


class TestArtifactEndpoints:
    def test_lists_task_artifacts_and_fetches_content(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        artifact = daemon._artifact_store.put_text(
            task_id="task-artifact",
            kind=ArtifactKind.PR_BODY,
            name="PR body",
            text="Closes #1",
            media_type="text/markdown",
        )

        listed = client.get("/api/v1/tasks/task-artifact/artifacts")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [artifact.id]
        assert listed.json()[0]["kind"] == "pr_body"

        metadata = client.get(f"/api/v1/artifacts/{artifact.id}")
        assert metadata.status_code == 200
        assert metadata.json()["sha256"] == artifact.sha256

        content = client.get(f"/api/v1/artifacts/{artifact.id}/content")
        assert content.status_code == 200
        assert content.text == "Closes #1"
        assert content.headers["content-type"].startswith("text/markdown")

    def test_lists_work_item_artifacts_by_kind(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        daemon._artifact_store.put_text(
            work_item_id="wi-artifact",
            kind=ArtifactKind.METADATA,
            name="Metadata",
            text="{}",
            media_type="application/json",
        )
        diff = daemon._artifact_store.put_text(
            work_item_id="wi-artifact",
            kind=ArtifactKind.DIFF,
            name="Diff",
            text="diff --git a/x b/x",
        )

        listed = client.get("/api/v1/work-items/wi-artifact/artifacts?kind=diff")

        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [diff.id]

    def test_missing_artifact_returns_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/artifacts/missing")

        assert response.status_code == 404


class TestActionEndpoints:
    def test_lists_and_fetches_task_actions(self, client: TestClient, daemon: Daemon) -> None:
        action = daemon.propose_action(
            task_id="task-action",
            kind=ActionKind.FILE_WRITE,
            summary="write file",
            payload={"path": "ok.py"},
        )

        listed = client.get("/api/v1/tasks/task-action/actions")
        detail = client.get(f"/api/v1/actions/{action.id}")

        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [action.id]
        assert detail.status_code == 200
        assert detail.json()["summary"] == "write file"

    def test_lists_actions_queue_across_tasks(self, client: TestClient, daemon: Daemon) -> None:
        proposed = daemon.propose_action(
            task_id="task-action-a",
            work_item_id="wi-approval",
            kind=ActionKind.FILE_WRITE,
            summary="write file",
            payload={"path": "ok.py"},
        )
        approved = daemon.propose_action(
            task_id="task-action-b",
            work_item_id="wi-approval",
            kind=ActionKind.COMMAND,
            summary="run tests",
            payload={"command": "pytest"},
        )
        daemon.approve_action(approved.id, actor="test")

        listed = client.get(
            "/api/v1/actions",
            params={"status": "proposed", "work_item_id": "wi-approval"},
        )

        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [proposed.id]
        assert listed.json()[0]["approval_contract"] == "proposal_only"

    def test_approve_and_reject_actions(self, client: TestClient, daemon: Daemon) -> None:
        approved = daemon.propose_action(
            task_id="task-action",
            kind=ActionKind.FILE_WRITE,
            summary="write file",
            payload={"path": "ok.py"},
        )
        rejected = daemon.propose_action(
            task_id="task-action",
            kind=ActionKind.COMMAND,
            summary="run tests",
            payload={"command": "pytest"},
        )

        approve_response = client.post(f"/api/v1/actions/{approved.id}/approve")
        reject_response = client.post(
            f"/api/v1/actions/{rejected.id}/reject",
            json={"reason": "not now"},
        )

        assert approve_response.status_code == 200
        assert approve_response.json()["status"] == "approved"
        assert approve_response.json()["approval_contract"] == "proposal_only"
        assert reject_response.status_code == 200
        assert reject_response.json()["status"] == "rejected"
        assert reject_response.json()["rejection_reason"] == "not now"


class TestJwtAuthEndpoint:
    def test_whoami_returns_static_token_identity(self, auth_client: TestClient) -> None:
        r = auth_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer secret-abc"},  # nosec B106 — test fixture token matching auth_client fixture
        )

        assert r.status_code == 200
        assert r.json() == {"sub": "static-token", "role": "admin", "exp": None}

    def test_whoami_returns_anonymous_without_auth(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/v1/auth/me")

        assert r.status_code == 200
        assert r.json() == {"sub": "anonymous", "role": None, "exp": None}

    def test_whoami_rejects_wrong_static_token_identity(self, auth_client: TestClient) -> None:
        r = auth_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer wrong"},
        )

        assert r.status_code == 200
        assert r.json() == {"sub": "anonymous", "role": None, "exp": None}


class TestBatchDispatchEndpoint:
    def test_batch_dispatch_queues_each_issue(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/issues/batch-dispatch",
            json={
                "items": [
                    {"repo": "owner/repo", "number": 1, "mode": "plan"},
                    {
                        "repo": "owner/repo",
                        "number": 2,
                        "mode": "implement",
                        "backend": "primary",
                        "model": "test-model",
                    },
                ]
            },
        )

        assert r.status_code == 202
        body = r.json()
        assert body["dispatched"] == 2
        assert body["failed"] == 0
        assert [task["issue_number"] for task in body["tasks"]] == [1, 2]
        assert body["failures"] == []

    def test_batch_dispatch_records_item_failures(
        self,
        client: TestClient,
        daemon: Daemon,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_submit_issue = daemon.submit_issue

        def flaky_submit_issue(**kwargs: Any) -> Task:
            if kwargs["issue_number"] == 2:
                raise ValueError("backend unavailable")
            return original_submit_issue(**kwargs)

        monkeypatch.setattr(daemon, "submit_issue", flaky_submit_issue)

        r = client.post(
            "/api/v1/issues/batch-dispatch",
            json={
                "items": [
                    {"repo": "owner/repo", "number": 1},
                    {"repo": "owner/repo", "number": 2},
                ]
            },
        )

        assert r.status_code == 202
        body = r.json()
        assert body["dispatched"] == 1
        assert body["failed"] == 1
        assert body["failures"] == [
            {"repo": "owner/repo", "number": 2, "error": "backend unavailable"}
        ]


class TestFleetEndpoint:
    def test_merges_manifest_defaults_with_live_task_counts(
        self,
        client: TestClient,
        daemon: Daemon,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fleet_config = tmp_path / "fleet.yaml"
        fleet_config.write_text(
            """
fleet:
  name: desktop-fleet
  default_slots: 4
  default_budget_per_story: 1.25
  default_pr_target_branch: main
  default_watch_labels: [deliver, maxwell]
  auto_promote_staging: true
  discovery_interval_seconds: 120
repos:
  - org: D-sorganization
    name: Maxwell-Daemon
  - org: D-sorganization
    name: Other
    slots: 1
    budget_per_story: 0.25
    pr_target_branch: release
    watch_labels: [custom]
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("MAXWELL_FLEET_CONFIG", str(fleet_config))
        issue_task = Task(
            id="issue-1",
            prompt="D-sorganization/Maxwell-Daemon#168",
            kind=TaskKind.ISSUE,
            issue_repo="D-sorganization/Maxwell-Daemon",
            issue_number=168,
            status=TaskStatus.QUEUED,
            cost_usd=0.1234567,
        )
        repo_task = Task(
            id="repo-1",
            prompt="maintenance",
            repo="Other",
            status=TaskStatus.COMPLETED,
            cost_usd=0.5,
        )
        with daemon._tasks_lock:
            daemon._tasks[issue_task.id] = issue_task
            daemon._tasks[repo_task.id] = repo_task

        r = client.get("/api/v1/fleet")

        assert r.status_code == 200
        body = r.json()
        assert body["fleet"] == {
            "name": "desktop-fleet",
            "auto_promote_staging": True,
            "discovery_interval_seconds": 120,
        }
        maxwell, other = body["repos"]
        assert maxwell["github_url"] == "https://github.com/D-sorganization/Maxwell-Daemon"
        assert maxwell["slots"] == 4
        assert maxwell["budget_per_story"] == 1.25
        assert maxwell["pr_target_branch"] == "main"
        assert maxwell["watch_labels"] == ["deliver", "maxwell"]
        assert maxwell["active_tasks"] == 1
        assert maxwell["total_cost_usd"] == 0.123457
        assert other["slots"] == 1
        assert other["active_tasks"] == 0
        assert other["total_cost_usd"] == 0.5

    def test_reads_fleet_manifest_as_utf8(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fleet_config = tmp_path / "fleet.yaml"
        fleet_config.write_text(
            """
# UTF-8 marker: em dash — should parse on Windows and Linux
fleet:
  name: desktop-fleet
repos:
  - org: D-sorganization
    name: Maxwell-Daemon
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("MAXWELL_FLEET_CONFIG", str(fleet_config))

        r = client.get("/api/v1/fleet")

        assert r.status_code == 200
        assert r.json()["fleet"]["name"] == "desktop-fleet"


class TestFleetCapabilityRegistryEndpoint:
    def test_returns_redacted_registry_snapshot(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        daemon.fleet_registry.register(
            FleetNode(
                node_id="node-a",
                hostname="alpha",
                capabilities=(
                    NodeCapability(name="gpu", observed_at=datetime.now(timezone.utc), value=8),
                    NodeCapability(
                        name="secret-capability",
                        observed_at=datetime.now(timezone.utc),
                        value="hidden",
                    ),
                ),
                resource_snapshot=NodeResourceSnapshot(
                    captured_at=datetime.now(timezone.utc),
                    heartbeat_at=datetime.now(timezone.utc),
                    active_sessions=0,
                ),
                policy=NodePolicy(
                    allowed_repos=frozenset({"acme/repo"}),
                    allowed_tools=frozenset({"dispatch"}),
                    max_concurrent_sessions=2,
                    heartbeat_stale_after_seconds=600,
                ),
                tailscale_status=TailscalePeerStatus(
                    peer_id="node-a",
                    hostname="alpha",
                    online=True,
                    tailnet_ip="100.64.0.10",
                    current_address="100.64.0.10:41641",
                    last_seen_at=datetime.now(timezone.utc),
                ),
            )
        )

        response = client.get(
            "/api/v1/fleet/capabilities",
            params={
                "repo": "acme/repo",
                "tool": "dispatch",
                "required_capability": ["gpu"],
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["selected_node"]["hostname"] == "alpha"
        assert body["nodes"][0]["policy"] == {
            "has_repo_allowlist": True,
            "has_tool_allowlist": True,
            "allowed_repo_count": 1,
            "allowed_tool_count": 1,
            "max_concurrent_sessions": 2,
            "heartbeat_stale_after_seconds": 600,
        }
        assert body["nodes"][0]["capabilities"][0]["name"] == "gpu"
        assert "tailnet_ip" not in body["nodes"][0]["tailscale_status"]
        assert "current_address" not in body["nodes"][0]["tailscale_status"]

    def test_fleet_nodes_route_aliases_registry_snapshot(
        self,
        client: TestClient,
        daemon: Daemon,
    ) -> None:
        daemon.fleet_registry.register(
            FleetNode(
                node_id="node-a",
                hostname="alpha",
                capabilities=(NodeCapability(name="gpu", observed_at=datetime.now(timezone.utc)),),
                resource_snapshot=NodeResourceSnapshot(
                    captured_at=datetime.now(timezone.utc),
                    heartbeat_at=datetime.now(timezone.utc),
                    active_sessions=0,
                ),
                policy=NodePolicy(
                    allowed_repos=frozenset({"acme/repo"}),
                    allowed_tools=frozenset({"dispatch"}),
                    max_concurrent_sessions=2,
                    heartbeat_stale_after_seconds=600,
                ),
                tailscale_status=TailscalePeerStatus(
                    peer_id="node-a",
                    hostname="alpha",
                    online=True,
                    tailnet_ip="100.64.0.10",
                    current_address="100.64.0.10:41641",
                    last_seen_at=datetime.now(timezone.utc),
                ),
            )
        )

        response = client.get(
            "/api/v1/fleet/nodes",
            params={"repo": "acme/repo", "tool": "dispatch", "required_capability": ["gpu"]},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["selected_node"]["hostname"] == "alpha"
        assert "tailnet_ip" not in body["nodes"][0]["tailscale_status"]
        assert "current_address" not in body["nodes"][0]["tailscale_status"]


class TestAuditEndpoint:
    def test_audit_log_reports_disabled_when_not_configured(self, client: TestClient) -> None:
        r = client.get("/api/v1/audit")

        assert r.status_code == 200
        assert r.json() == {"entries": [], "audit_enabled": False}

    def test_audit_verify_reports_clean_when_not_configured(self, client: TestClient) -> None:
        r = client.get("/api/v1/audit/verify")

        assert r.status_code == 200
        assert r.json() == {"clean": True, "violations": [], "audit_enabled": False}


class TestWebhookEndpoint:
    def test_github_webhook_reports_disabled_without_secret(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/webhooks/github",
            content=b"{}",
            headers={"x-github-event": "ping"},
        )

        assert r.status_code == 503
        assert r.json() == {"detail": "webhooks disabled", "disabled": True}


class TestAuth:
    def test_protected_endpoint_rejects_missing_token(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/v1/backends")
        assert r.status_code == 401

    def test_rejects_malformed_bearer(self, auth_client: TestClient) -> None:
        r = auth_client.get(
            "/api/v1/backends",
            headers={"Authorization": "NotBearer secret-abc"},
        )
        assert r.status_code == 401

    def test_rejects_wrong_token(self, auth_client: TestClient) -> None:
        r = auth_client.get(
            "/api/v1/backends",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert r.status_code == 401

    def test_accepts_correct_token(self, auth_client: TestClient) -> None:
        r = auth_client.get(
            "/api/v1/backends",
            headers={"Authorization": "Bearer secret-abc"},  # nosec B106 — test fixture token matching auth_client fixture
        )
        assert r.status_code == 200


class TestSSHEndpointsWithoutAsyncSSH:
    """Issue #231 — SSH endpoints must return 503 when asyncssh is absent.

    The old guard only caught ImportError from importing SSHSessionPool, but
    maxwell_daemon.ssh.session imports successfully regardless of asyncssh.
    The fix adds an explicit ``import asyncssh`` check inside _ssh_pool() so
    the None sentinel is set correctly and the 503 guard fires.
    """

    def test_ssh_sessions_returns_503_when_asyncssh_absent(self, daemon: Daemon) -> None:
        import sys
        from unittest.mock import patch

        # Simulate asyncssh being absent by making it unimportable.
        with (
            patch.dict(sys.modules, {"asyncssh": None}),
            TestClient(create_app(daemon)) as c,
        ):
            r = c.get("/api/v1/ssh/sessions")
        assert r.status_code == 503
        assert "SSH support not installed" in r.json()["detail"]

    def test_ssh_connect_returns_503_when_asyncssh_absent(self, daemon: Daemon) -> None:
        import sys
        from unittest.mock import patch

        with (
            patch.dict(sys.modules, {"asyncssh": None}),
            TestClient(create_app(daemon)) as c,
        ):
            r = c.post(
                "/api/v1/ssh/connect",
                json={"host": "srv", "user": "ubuntu"},
            )
        assert r.status_code == 503
