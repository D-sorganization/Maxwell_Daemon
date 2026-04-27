"""`maxwell-daemon tasks ...` subcommand group."""

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
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
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
        timeout: float | None = None,
    ) -> _FakeResponse:
        calls.append({"method": "GET", "url": url, "headers": headers})
        return _Holder.response

    def post(
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        return _Holder.response

    import httpx

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    calls.append({"_holder": _Holder})  # pass holder back to test by convention
    return calls


class TestTasksList:
    def test_empty_list(self, runner: CliRunner, patch_httpx: list[dict[str, Any]]) -> None:
        holder = patch_httpx[-1]["_holder"]
        holder.response = _FakeResponse(payload=[])
        r = runner.invoke(app, ["tasks", "list"])
        assert r.exit_code == 0
        assert "No tasks" in r.stdout or "no tasks" in r.stdout.lower()

    def test_renders_table(self, runner: CliRunner, patch_httpx: list[dict[str, Any]]) -> None:
        holder = patch_httpx[-1]["_holder"]
        holder.response = _FakeResponse(
            payload=[
                {
                    "id": "abc",
                    "kind": "issue",
                    "status": "completed",
                    "prompt": "fix parser",
                    "repo": "o/r",
                    "issue_repo": "o/r",
                    "issue_number": 1,
                    "pr_url": "https://github.com/o/r/pull/1",
                    "cost_usd": 0.05,
                    "created_at": "2026-04-19T00:00:00Z",
                    "started_at": None,
                    "finished_at": None,
                    "backend": "claude",
                    "model": "claude-sonnet",
                    "result": None,
                    "error": None,
                    "issue_mode": "plan",
                }
            ]
        )
        r = runner.invoke(app, ["tasks", "list"])
        assert r.exit_code == 0
        assert "abc" in r.stdout
        assert "completed" in r.stdout

    def test_filter_by_status(self, runner: CliRunner, patch_httpx: list[dict[str, Any]]) -> None:
        holder = patch_httpx[-1]["_holder"]
        holder.response = _FakeResponse(payload=[])
        r = runner.invoke(app, ["tasks", "list", "--status", "queued"])
        assert r.exit_code == 0
        # The URL should carry the filter.
        api_calls = [c for c in patch_httpx if c.get("method") == "GET"]
        assert any("status=queued" in c["url"] for c in api_calls)


class TestTasksShow:
    def test_not_found(self, runner: CliRunner, patch_httpx: list[dict[str, Any]]) -> None:
        holder = patch_httpx[-1]["_holder"]
        holder.response = _FakeResponse(status_code=404, payload={"detail": "nope"})
        r = runner.invoke(app, ["tasks", "show", "missing"])
        assert r.exit_code == 1

    def test_shows_task_detail(self, runner: CliRunner, patch_httpx: list[dict[str, Any]]) -> None:
        holder = patch_httpx[-1]["_holder"]
        holder.response = _FakeResponse(
            payload={
                "id": "abc",
                "kind": "issue",
                "status": "completed",
                "prompt": "fix parser",
                "repo": "o/r",
                "issue_repo": "o/r",
                "issue_number": 7,
                "pr_url": "https://github.com/o/r/pull/42",
                "cost_usd": 0.123,
                "created_at": "2026-04-19T00:00:00Z",
                "started_at": "2026-04-19T00:00:01Z",
                "finished_at": "2026-04-19T00:00:05Z",
                "backend": "claude",
                "model": "claude-sonnet",
                "result": "plan text",
                "error": None,
                "issue_mode": "plan",
            }
        )
        r = runner.invoke(app, ["tasks", "show", "abc"])
        assert r.exit_code == 0
        assert "fix parser" in r.stdout
        assert "claude-sonnet" in r.stdout
        assert "pull/42" in r.stdout


class TestTasksCancel:
    def test_cancel_success(self, runner: CliRunner, patch_httpx: list[dict[str, Any]]) -> None:
        holder = patch_httpx[-1]["_holder"]
        holder.response = _FakeResponse(payload={"id": "abc", "status": "cancelled"})
        r = runner.invoke(app, ["tasks", "cancel", "abc"])
        assert r.exit_code == 0
        assert "cancelled" in r.stdout.lower()

    def test_cancel_failure_nonzero_exit(
        self, runner: CliRunner, patch_httpx: list[dict[str, Any]]
    ) -> None:
        holder = patch_httpx[-1]["_holder"]
        holder.response = _FakeResponse(status_code=409, payload={"detail": "already done"})
        r = runner.invoke(app, ["tasks", "cancel", "abc"])
        assert r.exit_code == 1
