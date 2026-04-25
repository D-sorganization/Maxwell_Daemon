"""`maxwell-daemon work-item ...` subcommand group."""

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

            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]


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
        params: dict | None = None,  # type: ignore[type-arg]
        headers: dict | None = None,  # type: ignore[type-arg]
        timeout: float | None = None,
    ) -> _FakeResponse:
        calls.append(
            {"method": "GET", "url": url, "params": params, "headers": headers}
        )
        return _Holder.response

    def post(
        url: str,
        *,
        json: dict | None = None,  # type: ignore[type-arg]
        headers: dict | None = None,  # type: ignore[type-arg]
        timeout: float | None = None,
    ) -> _FakeResponse:
        calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        return _Holder.response

    import httpx

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    calls.append({"_holder": _Holder})
    return calls


def test_create_posts_work_item(
    runner: CliRunner,
    patch_httpx: list[dict[str, Any]],
) -> None:
    holder = patch_httpx[-1]["_holder"]
    holder.response = _FakeResponse(payload={"id": "wi-1", "title": "Ship"})

    result = runner.invoke(
        app,
        ["work-item", "create", "Ship", "--criterion", "Has tests", "--priority", "10"],
    )

    assert result.exit_code == 0
    call = next(call for call in patch_httpx if call.get("method") == "POST")
    assert call["url"].endswith("/api/v1/work-items")
    assert call["json"]["acceptance_criteria"] == [{"id": "AC1", "text": "Has tests"}]


def test_list_renders_work_items(
    runner: CliRunner,
    patch_httpx: list[dict[str, Any]],
) -> None:
    holder = patch_httpx[-1]["_holder"]
    holder.response = _FakeResponse(
        payload=[
            {
                "id": "wi-1",
                "title": "Ship",
                "status": "draft",
                "priority": 10,
                "repo": "D-sorganization/Maxwell-Daemon",
            }
        ]
    )

    result = runner.invoke(app, ["work-item", "list", "--status", "draft"])

    assert result.exit_code == 0
    assert "wi-1" in result.stdout
    call = next(call for call in patch_httpx if call.get("method") == "GET")
    assert call["params"]["status"] == "draft"


def test_cancel_posts_transition(
    runner: CliRunner,
    patch_httpx: list[dict[str, Any]],
) -> None:
    holder = patch_httpx[-1]["_holder"]
    holder.response = _FakeResponse(payload={"id": "wi-1", "status": "cancelled"})

    result = runner.invoke(app, ["work-item", "cancel", "wi-1"])

    assert result.exit_code == 0
    call = next(call for call in patch_httpx if call.get("method") == "POST")
    assert call["url"].endswith("/api/v1/work-items/wi-1/transition")
    assert call["json"] == {"status": "cancelled"}
