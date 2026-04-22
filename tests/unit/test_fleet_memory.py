"""Remote fleet memory behavior."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

import httpx
import pytest

from maxwell_daemon.fleet.memory import RemoteMemoryManager


class _Response:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self._payload = payload
        self._status_code = status_code

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self._status_code >= 400:
            raise httpx.HTTPStatusError(
                "bad status",
                request=httpx.Request("POST", "https://coordinator.test"),
                response=httpx.Response(self._status_code),
            )


class _Client:
    requests: ClassVar[list[dict[str, Any]]] = []
    response: ClassVar[_Response | Exception] = _Response({"context": "shared context"})

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, object],
        headers: Mapping[str, str],
        timeout: float,
    ) -> _Response:
        self.requests.append(
            {"url": url, "json": dict(json), "headers": dict(headers), "timeout": timeout}
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture(autouse=True)
def _reset_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _Client.requests = []
    _Client.response = _Response({"context": "shared context"})
    monkeypatch.setattr(httpx, "Client", _Client)


def test_assemble_context_merges_remote_context_and_local_scratchpad() -> None:
    manager = RemoteMemoryManager("https://coordinator.test/", auth_token="token")
    manager.scratchpad.append("task-1", role="plan", content="local note")

    assembled = manager.assemble_context(
        repo="D-sorganization/Maxwell-Daemon",
        issue_title="title",
        issue_body="body",
        task_id="task-1",
    )

    assert "shared context" in assembled
    assert "local note" in assembled
    assert _Client.requests[0]["url"] == "https://coordinator.test/api/v1/memory/assemble"
    assert _Client.requests[0]["headers"]["Authorization"] == "Bearer token"


def test_assemble_context_falls_back_to_scratchpad_when_remote_fails() -> None:
    _Client.response = httpx.ConnectError("offline")
    manager = RemoteMemoryManager("https://coordinator.test")
    manager.scratchpad.append("task-1", role="plan", content="offline note")

    assembled = manager.assemble_context(
        repo="D-sorganization/Maxwell-Daemon",
        issue_title="title",
        issue_body="body",
        task_id="task-1",
    )

    assert "offline note" in assembled


def test_record_outcome_posts_to_coordinator_and_clears_scratchpad() -> None:
    manager = RemoteMemoryManager("https://coordinator.test")
    manager.scratchpad.append("task-1", role="plan", content="done")

    manager.record_outcome(
        task_id="task-1",
        repo="D-sorganization/Maxwell-Daemon",
        issue_number=296,
        issue_title="title",
        issue_body="body",
        plan="plan",
        applied_diff=True,
        pr_url="https://github.com/D-sorganization/Maxwell-Daemon/pull/303",
        outcome="completed",
    )

    assert _Client.requests[0]["url"] == "https://coordinator.test/api/v1/memory/record"
    assert _Client.requests[0]["json"]["issue_number"] == 296
    assert manager.scratchpad.entries("task-1") == []
