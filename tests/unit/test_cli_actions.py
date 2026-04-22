"""CLI coverage for action ledger commands."""

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
                request=None,
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
        url: str, *, headers: dict[str, str] | None = None, timeout: float | None = None
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
    calls.append({"_holder": _Holder})
    return calls


def _action_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "id": "act-1",
        "task_id": "task-1",
        "work_item_id": None,
        "kind": "file_write",
        "status": "proposed",
        "summary": "write file",
        "payload": {"path": "ok.py"},
        "risk_level": "medium",
        "requires_approval": True,
        "approved_by": None,
        "approved_at": None,
        "rejected_by": None,
        "rejected_at": None,
        "rejection_reason": None,
        "result_artifact_id": None,
        "result": {},
        "error": None,
        "created_at": "2026-04-22T00:00:00Z",
        "updated_at": "2026-04-22T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_task_actions_lists_actions(
    runner: CliRunner,
    patch_httpx: list[dict[str, Any]],
) -> None:
    holder = patch_httpx[-1]["_holder"]
    holder.response = _FakeResponse(payload=[_action_payload()])

    result = runner.invoke(app, ["tasks", "actions", "task-1"])

    assert result.exit_code == 0
    assert "act-1" in result.stdout
    assert "write file" in result.stdout


def test_action_show_fetches_action(
    runner: CliRunner,
    patch_httpx: list[dict[str, Any]],
) -> None:
    holder = patch_httpx[-1]["_holder"]
    holder.response = _FakeResponse(payload=_action_payload())

    result = runner.invoke(app, ["action", "show", "act-1"])

    assert result.exit_code == 0
    assert "file_write" in result.stdout


def test_action_approve_posts_decision(
    runner: CliRunner,
    patch_httpx: list[dict[str, Any]],
) -> None:
    holder = patch_httpx[-1]["_holder"]
    holder.response = _FakeResponse(payload=_action_payload(status="approved"))

    result = runner.invoke(app, ["action", "approve", "act-1"])

    assert result.exit_code == 0
    assert "approved" in result.stdout
    assert any(
        call.get("url", "").endswith("/api/v1/actions/act-1/approve") for call in patch_httpx
    )


def test_action_reject_posts_reason(
    runner: CliRunner,
    patch_httpx: list[dict[str, Any]],
) -> None:
    holder = patch_httpx[-1]["_holder"]
    holder.response = _FakeResponse(payload=_action_payload(status="rejected"))

    result = runner.invoke(app, ["action", "reject", "act-1", "--reason", "no"])

    assert result.exit_code == 0
    assert any(call.get("json") == {"reason": "no"} for call in patch_httpx)
