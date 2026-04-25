"""`maxwell-daemon delegate ...` subcommand group."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from maxwell_daemon.cli.main import app


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "err",
                request=httpx.Request("GET", "http://example.invalid"),
                response=httpx.Response(self.status_code),
            )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def patch_httpx(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class _Holder:
        response: _FakeResponse = _FakeResponse(payload=[])

    def get(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        calls.append(
            {"method": "GET", "url": url, "headers": headers, "params": params}
        )
        return _Holder.response

    import httpx

    monkeypatch.setattr(httpx, "get", get)
    calls.append({"_holder": _Holder})  # pass holder back to test by convention
    return calls


class TestDelegateList:
    def test_renders_table(
        self, runner: CliRunner, patch_httpx: list[dict[str, Any]]
    ) -> None:
        holder = patch_httpx[-1]["_holder"]
        holder.response = _FakeResponse(
            payload=[
                {
                    "session": {
                        "id": "session-1",
                        "delegate_id": "delegate-1",
                        "work_item_id": "issue-395",
                        "task_id": None,
                        "workspace_ref": "worktree://issue-395",
                        "backend_ref": "codex-cli",
                        "machine_ref": "worker-a",
                        "status": "running",
                        "active_lease_id": "session-1:worker-a:2026-04-22T12:00:00+00:00",
                        "prior_session_id": None,
                        "latest_checkpoint_id": "checkpoint-1",
                        "recovered_at": None,
                        "created_at": "2026-04-22T11:00:00+00:00",
                        "updated_at": "2026-04-22T12:00:00+00:00",
                        "metadata": {},
                    },
                    "active_lease": {
                        "id": "session-1:worker-a:2026-04-22T12:00:00+00:00",
                        "session_id": "session-1",
                        "owner_id": "worker-a",
                        "heartbeat_at": "2026-04-22T12:00:00+00:00",
                        "expires_at": "2026-04-22T12:05:00+00:00",
                        "renewal_count": 0,
                        "recovery_policy": "recoverable",
                        "released_at": None,
                        "expired_at": None,
                        "supersedes_owner_id": None,
                    },
                    "latest_checkpoint": {
                        "id": "checkpoint-1",
                        "session_id": "session-1",
                        "created_at": "2026-04-22T12:02:00+00:00",
                        "current_plan": "Keep a durable checkpoint for recovery.",
                        "changed_files": ["maxwell_daemon/core/delegate_lifecycle.py"],
                        "test_commands": ["pytest tests/unit/test_cli_delegates.py"],
                        "failures_and_learnings": [
                            "CLI should render the latest checkpoint."
                        ],
                        "artifact_refs": ["artifact://patch-4"],
                        "resume_prompt": "Resume from the last checkpoint.",
                        "metadata": {},
                    },
                    "handoff_artifacts": [],
                }
            ]
        )
        r = runner.invoke(app, ["delegate", "list"])
        assert r.exit_code == 0
        assert "running" in r.stdout
        assert "worker-a" in r.stdout

    def test_filters_query_params(
        self, runner: CliRunner, patch_httpx: list[dict[str, Any]]
    ) -> None:
        holder = patch_httpx[-1]["_holder"]
        holder.response = _FakeResponse(payload=[])
        r = runner.invoke(
            app,
            ["delegate", "list", "--work-item-id", "issue-395", "--status", "running"],
        )
        assert r.exit_code == 0
        api_calls = [c for c in patch_httpx if c.get("method") == "GET"]
        assert any("work_item_id" in str(c.get("params")) for c in api_calls)
        assert any("status" in str(c.get("params")) for c in api_calls)


class TestDelegateShow:
    def test_shows_session_detail(
        self, runner: CliRunner, patch_httpx: list[dict[str, Any]]
    ) -> None:
        holder = patch_httpx[-1]["_holder"]
        holder.response = _FakeResponse(
            payload={
                "session": {
                    "id": "session-1",
                    "delegate_id": "delegate-1",
                    "work_item_id": "issue-395",
                    "task_id": None,
                    "workspace_ref": "worktree://issue-395",
                    "backend_ref": "codex-cli",
                    "machine_ref": "worker-a",
                    "status": "running",
                    "active_lease_id": "session-1:worker-a:2026-04-22T12:00:00+00:00",
                    "prior_session_id": None,
                    "latest_checkpoint_id": "checkpoint-1",
                    "recovered_at": None,
                    "created_at": "2026-04-22T11:00:00+00:00",
                    "updated_at": "2026-04-22T12:00:00+00:00",
                    "metadata": {},
                },
                "active_lease": {
                    "id": "session-1:worker-a:2026-04-22T12:00:00+00:00",
                    "session_id": "session-1",
                    "owner_id": "worker-a",
                    "heartbeat_at": "2026-04-22T12:00:00+00:00",
                    "expires_at": "2026-04-22T12:05:00+00:00",
                    "renewal_count": 0,
                    "recovery_policy": "recoverable",
                    "released_at": None,
                    "expired_at": None,
                    "supersedes_owner_id": None,
                },
                "latest_checkpoint": {
                    "id": "checkpoint-1",
                    "session_id": "session-1",
                    "created_at": "2026-04-22T12:02:00+00:00",
                    "current_plan": "Keep a durable checkpoint for recovery.",
                    "changed_files": ["maxwell_daemon/core/delegate_lifecycle.py"],
                    "test_commands": ["pytest tests/unit/test_cli_delegates.py"],
                    "failures_and_learnings": [
                        "CLI should render the latest checkpoint."
                    ],
                    "artifact_refs": ["artifact://patch-4"],
                    "resume_prompt": "Resume from the last checkpoint.",
                    "metadata": {},
                },
                "handoff_artifacts": [],
            }
        )
        r = runner.invoke(app, ["delegate", "show", "session-1"])
        assert r.exit_code == 0
        assert "session-1" in r.stdout
        assert "checkpoint-1" in r.stdout
        assert "Resume from the last checkpoint." in r.stdout
