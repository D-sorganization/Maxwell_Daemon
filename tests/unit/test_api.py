"""FastAPI REST server — endpoint contracts, auth, and error paths."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.core.actions import ActionKind
from maxwell_daemon.core.artifacts import ArtifactKind
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import Task, TaskKind, TaskStatus


@pytest.fixture
def daemon(
    minimal_config: MaxwellDaemonConfig,
    isolated_ledger_path,
    tmp_path: Path,
) -> Iterator[Daemon]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(
        minimal_config,
        ledger_path=isolated_ledger_path,
        task_store_path=tmp_path / "tasks.db",
        work_item_store_path=tmp_path / "work_items.db",
        artifact_store_path=tmp_path / "artifacts.db",
        artifact_blob_root=tmp_path / "artifacts",
        action_store_path=tmp_path / "actions.db",
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
        assert daemon._task_store.get("api-duplicate-id").prompt == "first"

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

    def test_get_nonexistent_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/v1/tasks/nonexistent-id")
        assert r.status_code == 404


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

        def flaky_submit_issue(**kwargs):
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
