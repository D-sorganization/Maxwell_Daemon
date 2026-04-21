"""Async HTTP client for dispatching tasks to remote daemon instances.

Transport is injected via :class:`HTTPClientProtocol` so tests can hand in a
recorder and never touch a real socket. Production code instantiates the
adapter with an :class:`httpx.AsyncClient`.

Two endpoints matter for now:

* ``POST /api/v1/tasks``  — submit a task for execution
* ``GET  /api/v1/health`` — liveness probe used by :meth:`refresh_all`

All methods translate transport-level failures (connection refused, timeouts)
into a single :class:`RemoteDaemonError` so callers don't have to know which
HTTP library we're using underneath.

See GitHub issue #104.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from maxwell_daemon.fleet.dispatcher import MachineState

__all__ = [
    "HTTPClientProtocol",
    "HTTPResponseProtocol",
    "RemoteDaemonClient",
    "RemoteDaemonError",
    "RemoteTaskResult",
]


_TASKS_PATH = "/api/v1/tasks"
_HEALTH_PATH = "/api/v1/health"


class RemoteDaemonError(RuntimeError):
    """Raised when a remote daemon call fails or times out at the transport layer."""


@dataclass(slots=True, frozen=True)
class RemoteTaskResult:
    """Outcome of a submit_task call."""

    task_id: str
    machine_name: str
    status: str  # "submitted" | "error"
    detail: str = ""


@runtime_checkable
class HTTPResponseProtocol(Protocol):
    """Shape of an HTTP response our client consumes.

    Matches httpx.Response closely enough that a real httpx client satisfies it
    without adaptation.
    """

    status_code: int

    def json(self) -> Any: ...


@runtime_checkable
class HTTPClientProtocol(Protocol):
    """Minimal async HTTP client surface the adapter needs."""

    async def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> HTTPResponseProtocol: ...

    async def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponseProtocol: ...


class RemoteDaemonClient:
    """Async HTTP client for dispatching tasks to remote daemon instances.

    The ``http_client`` is injected. Pass ``None`` to use a fresh
    :class:`httpx.AsyncClient` with the configured timeout.
    """

    def __init__(
        self,
        *,
        http_client: HTTPClientProtocol | None = None,
        auth_token: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._http = http_client if http_client is not None else _make_default_http(timeout_seconds)
        self._auth_token = auth_token
        self._timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------ headers
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    # ------------------------------------------------------------------ URLs
    @staticmethod
    def _base_url(machine: MachineState) -> str:
        return f"http://{machine.host}:{machine.port}"

    # ------------------------------------------------------------------ submit
    async def submit_task(
        self,
        machine: MachineState,
        *,
        task_payload: dict[str, Any],
    ) -> RemoteTaskResult:
        """POST the task payload; translate HTTP status into a typed result."""
        url = f"{self._base_url(machine)}{_TASKS_PATH}"
        task_id = str(task_payload.get("task_id", ""))
        try:
            response = await self._http.post(url, json=task_payload, headers=self._headers())
        except Exception as exc:  # transport-level failure
            raise RemoteDaemonError(
                f"submit_task to {machine.name} ({url}) failed: {exc!r}"
            ) from exc

        if 200 <= response.status_code < 300:
            return RemoteTaskResult(
                task_id=task_id,
                machine_name=machine.name,
                status="submitted",
            )

        return RemoteTaskResult(
            task_id=task_id,
            machine_name=machine.name,
            status="error",
            detail=_extract_error_detail(response),
        )

    # ------------------------------------------------------------------ health
    async def health_check(self, machine: MachineState) -> bool:
        """Liveness probe. Returns True iff the daemon answers 200."""
        url = f"{self._base_url(machine)}{_HEALTH_PATH}"
        try:
            response = await self._http.get(url, headers=self._headers())
        except Exception:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool.

        If the injected ``http_client`` exposes an ``aclose()`` coroutine (as
        the default :func:`_make_default_http` adapter does) it is awaited.
        Injected test doubles that omit ``aclose`` are silently ignored.
        """
        close = getattr(self._http, "aclose", None)
        if close is not None:
            await close()

    async def refresh_all(self, machines: tuple[MachineState, ...]) -> tuple[MachineState, ...]:
        """Probe every machine in parallel, return snapshots with ``healthy`` updated.

        One machine's failure never affects the probe of another — each task
        catches its own exceptions. Other fields (host, port, capacity, tags,
        active_tasks) pass through untouched.
        """
        if not machines:
            return ()
        results = await asyncio.gather(
            *(self.health_check(m) for m in machines),
            return_exceptions=False,
        )
        return tuple(
            MachineState(
                name=m.name,
                host=m.host,
                port=m.port,
                capacity=m.capacity,
                tags=m.tags,
                active_tasks=m.active_tasks,
                healthy=healthy,
            )
            for m, healthy in zip(machines, results, strict=True)
        )


def _extract_error_detail(response: HTTPResponseProtocol) -> str:
    """Pull a useful error message out of an HTTP error response.

    We try JSON first, fall back to ``.text`` (if present), and finally to just
    the status code so callers always have something non-empty to surface.
    """
    status = response.status_code
    try:
        body = response.json()
        if isinstance(body, dict):
            for key in ("error", "detail", "message"):
                if body.get(key):
                    return f"HTTP {status}: {body[key]}"
        return f"HTTP {status}: {body!r}"
    except Exception:
        text = getattr(response, "text", "") or ""
        if text:
            return f"HTTP {status}: {text}"
        return f"HTTP {status}"


def _make_default_http(timeout_seconds: float) -> HTTPClientProtocol:
    """Create an httpx AsyncClient wrapped to match our protocol.

    Imported lazily so unit tests can run without httpx being functional in the
    test environment (they always inject a fake).

    The adapter holds a single long-lived ``AsyncClient`` and exposes an
    ``aclose()`` method so callers (e.g. ``RemoteDaemonClient``) can release the
    underlying connection pool when they are done.
    """
    import httpx

    class _HttpxAdapter:
        def __init__(self, timeout: float) -> None:
            self._client = httpx.AsyncClient(timeout=timeout)

        async def post(
            self, url: str, *, json: dict[str, Any], headers: dict[str, str]
        ) -> HTTPResponseProtocol:
            return await self._client.post(url, json=json, headers=headers)

        async def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponseProtocol:
            return await self._client.get(url, headers=headers)

        async def aclose(self) -> None:
            await self._client.aclose()

    return _HttpxAdapter(timeout_seconds)
