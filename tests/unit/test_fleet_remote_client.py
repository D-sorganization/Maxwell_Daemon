"""Unit tests for maxwell_daemon.fleet.client — async HTTP client for remote daemons.

Follows the injected-runner pattern: tests pass a recording fake HTTP client so
no sockets are opened. The production code uses httpx; the test fakes emulate
just enough of httpx's response surface (`status_code`, `.json()`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from maxwell_daemon.fleet.client import (
    RemoteDaemonClient,
    RemoteDaemonError,
    RemoteTaskResult,
)
from maxwell_daemon.fleet.dispatcher import MachineState


def _machine(
    name: str = "m1", host: str = "host.example", port: int = 50051
) -> MachineState:
    return MachineState(
        name=name,
        host=host,
        port=port,
        capacity=2,
        tags=(),
        active_tasks=0,
        healthy=True,
    )


@dataclass
class FakeResponse:
    status_code: int
    _body: dict[str, Any] = field(default_factory=dict)
    _text: str = ""

    def json(self) -> dict[str, Any]:
        return self._body

    @property
    def text(self) -> str:
        return self._text or str(self._body)


@dataclass
class RecordedPost:
    url: str
    json: dict[str, Any]
    headers: dict[str, str]


@dataclass
class RecordedGet:
    url: str
    headers: dict[str, str]


class FakeHTTPClient:
    """Records calls and returns canned responses keyed by URL + method."""

    def __init__(
        self,
        *,
        post_responses: dict[str, FakeResponse] | None = None,
        get_responses: dict[str, FakeResponse] | None = None,
        post_exceptions: dict[str, Exception] | None = None,
        get_exceptions: dict[str, Exception] | None = None,
    ) -> None:
        self._post_responses = post_responses or {}
        self._get_responses = get_responses or {}
        self._post_exceptions = post_exceptions or {}
        self._get_exceptions = get_exceptions or {}
        self.posts: list[RecordedPost] = []
        self.gets: list[RecordedGet] = []

    async def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> FakeResponse:
        self.posts.append(RecordedPost(url=url, json=json, headers=dict(headers)))
        if url in self._post_exceptions:
            raise self._post_exceptions[url]
        return self._post_responses.get(
            url, FakeResponse(status_code=200, _body={"ok": True})
        )

    async def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
        self.gets.append(RecordedGet(url=url, headers=dict(headers)))
        if url in self._get_exceptions:
            raise self._get_exceptions[url]
        return self._get_responses.get(
            url, FakeResponse(status_code=200, _body={"ok": True})
        )


class TestSubmitTaskRequestShape:
    async def test_posts_to_correct_url(self) -> None:
        http = FakeHTTPClient()
        client = RemoteDaemonClient(http_client=http)
        await client.submit_task(
            _machine(host="runner-a", port=50099),
            task_payload={"task_id": "t1"},
        )
        assert len(http.posts) == 1
        assert http.posts[0].url == "https://runner-a:50099/api/v1/tasks"

    async def test_sends_json_body(self) -> None:
        http = FakeHTTPClient()
        client = RemoteDaemonClient(http_client=http)
        payload = {"task_id": "t1", "repo": "acme/foo"}
        await client.submit_task(_machine(), task_payload=payload)
        assert http.posts[0].json == payload

    async def test_no_auth_header_when_token_unset(self) -> None:
        http = FakeHTTPClient()
        client = RemoteDaemonClient(http_client=http)
        await client.submit_task(_machine(), task_payload={})
        assert "Authorization" not in http.posts[0].headers

    async def test_bearer_header_when_token_set(self) -> None:
        http = FakeHTTPClient()
        client = RemoteDaemonClient(http_client=http, auth_token="s3cret")
        await client.submit_task(_machine(), task_payload={})
        assert http.posts[0].headers["Authorization"] == "Bearer s3cret"


class TestSubmitTaskResponse:
    @pytest.mark.parametrize("status", [200, 201, 202])
    async def test_success_statuses_return_submitted(self, status: int) -> None:
        url = "https://host.example:50051/api/v1/tasks"
        http = FakeHTTPClient(
            post_responses={
                url: FakeResponse(status_code=status, _body={"accepted": True})
            }
        )
        client = RemoteDaemonClient(http_client=http)
        result = await client.submit_task(_machine(), task_payload={"task_id": "t1"})
        assert isinstance(result, RemoteTaskResult)
        assert result.status == "submitted"
        assert result.task_id == "t1"
        assert result.machine_name == "m1"

    @pytest.mark.parametrize("status", [400, 403, 404, 500, 503])
    async def test_error_statuses_return_error_with_detail(self, status: int) -> None:
        url = "https://host.example:50051/api/v1/tasks"
        http = FakeHTTPClient(
            post_responses={
                url: FakeResponse(
                    status_code=status, _body={"error": "nope"}, _text="nope"
                ),
            }
        )
        client = RemoteDaemonClient(http_client=http)
        result = await client.submit_task(_machine(), task_payload={"task_id": "t1"})
        assert result.status == "error"
        assert result.task_id == "t1"
        assert result.machine_name == "m1"
        assert result.detail  # non-empty detail on error
        assert str(status) in result.detail or "nope" in result.detail

    async def test_task_id_missing_from_payload_still_returns_result(self) -> None:
        """If the caller forgets ``task_id``, the result should still carry an id
        (we fall back to empty string rather than crashing)."""
        http = FakeHTTPClient()
        client = RemoteDaemonClient(http_client=http)
        result = await client.submit_task(_machine(), task_payload={})
        assert isinstance(result, RemoteTaskResult)
        assert result.status == "submitted"


class TestSubmitTaskTransport:
    async def test_transport_failure_raises_remote_daemon_error(self) -> None:
        url = "https://host.example:50051/api/v1/tasks"
        http = FakeHTTPClient(
            post_exceptions={url: ConnectionRefusedError("nope")},
        )
        client = RemoteDaemonClient(http_client=http)
        with pytest.raises(RemoteDaemonError):
            await client.submit_task(_machine(), task_payload={"task_id": "t1"})

    async def test_timeout_raises_remote_daemon_error(self) -> None:
        url = "https://host.example:50051/api/v1/tasks"
        http = FakeHTTPClient(
            post_exceptions={url: TimeoutError("slow")},
        )
        client = RemoteDaemonClient(http_client=http)
        with pytest.raises(RemoteDaemonError):
            await client.submit_task(_machine(), task_payload={"task_id": "t1"})


class TestHealthCheck:
    async def test_returns_true_on_200(self) -> None:
        url = "https://host.example:50051/api/v1/health"
        http = FakeHTTPClient(get_responses={url: FakeResponse(status_code=200)})
        client = RemoteDaemonClient(http_client=http)
        assert await client.health_check(_machine()) is True

    @pytest.mark.parametrize("status", [201, 301, 400, 500, 503])
    async def test_returns_false_on_non_200(self, status: int) -> None:
        url = "https://host.example:50051/api/v1/health"
        http = FakeHTTPClient(get_responses={url: FakeResponse(status_code=status)})
        client = RemoteDaemonClient(http_client=http)
        assert await client.health_check(_machine()) is False

    async def test_returns_false_on_transport_exception(self) -> None:
        url = "https://host.example:50051/api/v1/health"
        http = FakeHTTPClient(get_exceptions={url: ConnectionRefusedError("nope")})
        client = RemoteDaemonClient(http_client=http)
        assert await client.health_check(_machine()) is False

    async def test_sends_auth_header_when_configured(self) -> None:
        http = FakeHTTPClient()
        client = RemoteDaemonClient(http_client=http, auth_token="t0k")
        await client.health_check(_machine())
        assert http.gets[0].headers["Authorization"] == "Bearer t0k"


class TestRefreshAll:
    async def test_returns_same_number_of_machines(self) -> None:
        http = FakeHTTPClient()
        client = RemoteDaemonClient(http_client=http)
        machines = (
            _machine("a", host="ha"),
            _machine("b", host="hb"),
            _machine("c", host="hc"),
        )
        result = await client.refresh_all(machines)
        assert len(result) == 3

    async def test_updates_healthy_flag(self) -> None:
        http = FakeHTTPClient(
            get_responses={
                "https://ha:50051/api/v1/health": FakeResponse(status_code=200),
                "https://hb:50051/api/v1/health": FakeResponse(status_code=500),
            }
        )
        client = RemoteDaemonClient(http_client=http)
        machines = (_machine("a", host="ha"), _machine("b", host="hb"))
        result = await client.refresh_all(machines)
        by_name = {m.name: m for m in result}
        assert by_name["a"].healthy is True
        assert by_name["b"].healthy is False

    async def test_preserves_other_fields(self) -> None:
        http = FakeHTTPClient()
        client = RemoteDaemonClient(http_client=http)
        original = MachineState(
            name="a",
            host="ha",
            port=9000,
            capacity=5,
            tags=("gpu", "linux"),
            active_tasks=3,
            healthy=False,
        )
        result = await client.refresh_all((original,))
        updated = result[0]
        assert updated.name == "a"
        assert updated.host == "ha"
        assert updated.port == 9000
        assert updated.capacity == 5
        assert updated.tags == ("gpu", "linux")
        assert updated.active_tasks == 3

    async def test_one_failing_machine_does_not_affect_others(self) -> None:
        http = FakeHTTPClient(
            get_responses={
                "https://ha:50051/api/v1/health": FakeResponse(status_code=200),
                "https://hc:50051/api/v1/health": FakeResponse(status_code=200),
            },
            get_exceptions={
                "https://hb:50051/api/v1/health": ConnectionRefusedError("nope"),
            },
        )
        client = RemoteDaemonClient(http_client=http)
        machines = (
            _machine("a", host="ha"),
            _machine("b", host="hb"),
            _machine("c", host="hc"),
        )
        result = await client.refresh_all(machines)
        by_name = {m.name: m.healthy for m in result}
        assert by_name == {"a": True, "b": False, "c": True}

    async def test_probes_run_in_parallel(self) -> None:
        """Parallel probes: if we measure concurrency, N probes with delay D
        should complete in roughly D, not N*D."""

        class SlowHTTP:
            def __init__(self) -> None:
                self.active = 0
                self.max_active = 0

            async def post(
                self, url: str, *, json: dict[str, Any], headers: dict[str, str]
            ) -> FakeResponse:
                raise AssertionError("post not used in refresh_all")

            async def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                try:
                    await asyncio.sleep(0.05)
                    return FakeResponse(status_code=200)
                finally:
                    self.active -= 1

        http = SlowHTTP()
        client = RemoteDaemonClient(http_client=http)
        machines = tuple(_machine(f"m{i}", host=f"h{i}") for i in range(5))
        await client.refresh_all(machines)
        assert http.max_active >= 2, "probes should run concurrently"

    async def test_empty_input_returns_empty(self) -> None:
        http = FakeHTTPClient()
        client = RemoteDaemonClient(http_client=http)
        assert await client.refresh_all(()) == ()


class TestRemoteTaskResultFrozen:
    async def test_remote_task_result_frozen(self) -> None:
        import dataclasses as dc

        r = RemoteTaskResult(task_id="t1", machine_name="m1", status="submitted")
        with pytest.raises(dc.FrozenInstanceError):
            r.status = "error"  # type: ignore[misc]
