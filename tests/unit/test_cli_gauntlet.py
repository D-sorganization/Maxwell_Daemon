"""`maxwell-daemon gauntlet ...` and `maxwell-daemon gate ...` commands."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from maxwell_daemon.cli.main import app


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = {} if payload is None else payload

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
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        calls.append(
            {
                "method": "GET",
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return _Holder.response

    def post(
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        calls.append(
            {
                "method": "POST",
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return _Holder.response

    import httpx

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    calls.append({"_holder": _Holder})
    return calls


def _gauntlet_row(*, task_id: str = "task-1", status: str = "failed") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "title": "Fix gate runtime",
        "status": status,
        "final_decision": "fail" if status == "failed" else "running",
        "current_gate": "Verification",
        "next_action": "Inspect blocker evidence, then retry or waive with a reason",
        "gates": (
            {
                "id": "intake",
                "name": "Intake",
                "status": "passed",
                "next_action": "done",
            },
            {
                "id": "verification",
                "name": "Verification",
                "status": "failed" if status == "failed" else "running",
                "next_action": "Run tests again",
            },
        ),
        "critic_findings": (
            {
                "severity": "blocker",
                "title": "Tests failed",
                "detail": "pytest reported one regression",
            },
        ),
        "actions": (
            {
                "kind": "retry",
                "path": f"/api/v1/control-plane/gauntlet/{task_id}/retry",
                "target_id": task_id,
                "expected_status": "failed",
                "requires_reason": False,
                "requires_actor": False,
            },
            {
                "kind": "waive",
                "path": f"/api/v1/control-plane/gauntlet/{task_id}/waive",
                "target_id": task_id,
                "expected_status": "failed",
                "requires_reason": True,
                "requires_actor": True,
            },
        ),
    }


class TestGauntletList:
    def test_list_renders_rows(
        self, runner: CliRunner, patch_httpx: list[dict[str, Any]]
    ) -> None:
        holder = next(item["_holder"] for item in patch_httpx if "_holder" in item)
        holder.response = _FakeResponse(payload=[_gauntlet_row()])

        result = runner.invoke(app, ["gauntlet", "list"])

        assert result.exit_code == 0
        assert "task-1" in result.stdout
        assert "Verification" in result.stdout

    def test_gate_alias_and_filters_are_forwarded(
        self, runner: CliRunner, patch_httpx: list[dict[str, Any]]
    ) -> None:
        holder = next(item["_holder"] for item in patch_httpx if "_holder" in item)
        holder.response = _FakeResponse(payload=[])

        result = runner.invoke(
            app, ["gate", "list", "--task-id", "task-9", "--status", "failed"]
        )

        assert result.exit_code == 0
        call = next(call for call in patch_httpx if call.get("method") == "GET")
        assert call["params"] == {"limit": 25, "task_id": "task-9", "status": "failed"}


class TestGauntletStatus:
    def test_status_renders_gates_findings_and_actions(
        self, runner: CliRunner, patch_httpx: list[dict[str, Any]]
    ) -> None:
        holder = next(item["_holder"] for item in patch_httpx if "_holder" in item)
        holder.response = _FakeResponse(payload=[_gauntlet_row(task_id="task-22")])

        result = runner.invoke(app, ["gauntlet", "status", "task-22"])

        assert result.exit_code == 0
        assert "Critic Findings" in result.stdout
        assert "Available Actions" in result.stdout
        call = next(call for call in patch_httpx if call.get("method") == "GET")
        assert call["params"] == {"limit": 1, "task_id": "task-22"}

    def test_status_fails_when_task_missing(
        self, runner: CliRunner, patch_httpx: list[dict[str, Any]]
    ) -> None:
        holder = next(item["_holder"] for item in patch_httpx if "_holder" in item)
        holder.response = _FakeResponse(payload=[])

        result = runner.invoke(app, ["gauntlet", "status", "missing-task"])

        assert result.exit_code == 1
        assert "missing-task" in result.stdout


class TestGauntletActions:
    def test_retry_posts_expected_payload(
        self, runner: CliRunner, patch_httpx: list[dict[str, Any]]
    ) -> None:
        holder = next(item["_holder"] for item in patch_httpx if "_holder" in item)
        holder.response = _FakeResponse(
            payload={**_gauntlet_row(task_id="retry-me"), "status": "queued"}
        )

        result = runner.invoke(app, ["gauntlet", "retry", "retry-me"])

        assert result.exit_code == 0
        call = next(call for call in patch_httpx if call.get("method") == "POST")
        assert call["url"].endswith("/api/v1/control-plane/gauntlet/retry-me/retry")
        assert call["json"] == {"target_id": "retry-me", "expected_status": "failed"}

    def test_waive_posts_actor_and_reason(
        self, runner: CliRunner, patch_httpx: list[dict[str, Any]]
    ) -> None:
        holder = next(item["_holder"] for item in patch_httpx if "_holder" in item)
        holder.response = _FakeResponse(
            payload={**_gauntlet_row(task_id="waive-me"), "final_decision": "waived"}
        )

        result = runner.invoke(
            app,
            [
                "gate",
                "waive",
                "waive-me",
                "--actor",
                "reviewer",
                "--reason",
                "temporary exception",
            ],
        )

        assert result.exit_code == 0
        call = next(call for call in patch_httpx if call.get("method") == "POST")
        assert call["url"].endswith("/api/v1/control-plane/gauntlet/waive-me/waive")
        assert call["json"] == {
            "target_id": "waive-me",
            "expected_status": "failed",
            "actor": "reviewer",
            "reason": "temporary exception",
        }
