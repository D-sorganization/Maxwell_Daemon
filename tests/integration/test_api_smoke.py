"""Phase-1 contract smoke tests for the documented operator HTTP surface.

These tests exercise the **stable** operator endpoints documented in
``CLAUDE.md`` and ``maxwell_daemon/api/contract.py``:

* ``GET  /api/version``
* ``GET  /api/health``
* ``GET  /api/status``
* ``POST /api/dispatch``
* ``POST /api/control/{pause,resume,abort}``
* ``WS   /api/v1/events`` — at least one frame round-trip

The point is wiring + contract conformance, not LLM behaviour, so we boot
the real ``Daemon`` against a tmp-path SQLite and a stub ``recording``
backend (registered by the shared :func:`register_recording_backend`
fixture in ``tests/conftest.py``).  No network, no subprocess, no real
LLM.

Anything optional that pulls in heavy/external machinery (real
``asyncssh``, JWT auth) is skipped via ``pytest.importorskip`` per the
project hard rules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.api.contract import CONTRACT_VERSION
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon

# A short, well-known secret reused for both dispatch + control envelopes.
# The ``/api/dispatch`` and ``/api/control/*`` endpoints validate the
# ``confirmation_token`` against the static ``auth_token`` passed to
# ``create_app``, so the test must use the same value.
AUTH_TOKEN = "phase1-test-token"


@pytest.fixture
def smoke_system(
    tmp_path: Path, register_recording_backend: None
) -> Iterator[tuple[Daemon, TestClient, asyncio.AbstractEventLoop]]:
    """Boot a real Daemon + FastAPI app with a stub recording backend.

    Mirrors the pattern in ``tests/integration/test_end_to_end.py`` so the
    fixture surface is consistent across the integration suite.
    """

    cfg = MaxwellDaemonConfig.model_validate(
        {
            "backends": {
                "primary": {"type": "recording", "model": "smoke-model"},
            },
            "agent": {"default_backend": "primary"},
            "budget": {"monthly_limit_usd": 100.0, "hard_stop": False},
        }
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    daemon = Daemon(
        cfg,
        ledger_path=tmp_path / "ledger.db",
        task_store_path=tmp_path / "tasks.db",
    )
    loop.run_until_complete(daemon.start(worker_count=1))
    loop.run_until_complete(asyncio.sleep(0))

    app = create_app(daemon, auth_token=AUTH_TOKEN)
    with TestClient(app) as client:
        try:
            yield daemon, client, loop
        finally:
            loop.run_until_complete(daemon.stop())
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            asyncio.set_event_loop(None)


# ── /api/version ──────────────────────────────────────────────────────────


def test_api_version_returns_documented_schema(
    smoke_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop],
) -> None:
    """``GET /api/version`` returns ``{daemon, contract}`` per VersionResponse."""
    _, client, _ = smoke_system
    r = client.get("/api/version")
    assert r.status_code == 200, r.text

    body = r.json()
    # Contract: both fields present, both non-empty strings.
    assert set(body.keys()) >= {"daemon", "contract"}
    assert isinstance(body["daemon"], str) and body["daemon"]
    assert isinstance(body["contract"], str) and body["contract"]
    # Contract version must match what the module advertises — append-only,
    # so the major component is what dashboards key off.
    assert body["contract"] == CONTRACT_VERSION


# ── /api/health ───────────────────────────────────────────────────────────


def test_api_health_returns_liveness_and_gate(
    smoke_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop],
) -> None:
    """``GET /api/health`` returns ``{status, uptime_seconds, gate}``."""
    _, client, _ = smoke_system
    r = client.get("/api/health")
    assert r.status_code == 200, r.text

    body = r.json()
    # Required contract fields.
    assert body["status"] in {"ok", "degraded"}
    assert isinstance(body["uptime_seconds"], (int, float))
    assert body["uptime_seconds"] >= 0.0
    assert body["gate"] in {"open", "closed"}


# ── /api/status ───────────────────────────────────────────────────────────


def test_api_status_returns_pipeline_state(
    smoke_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop],
) -> None:
    """``GET /api/status`` returns pipeline state + active task fields."""
    _, client, _ = smoke_system
    r = client.get("/api/status")
    assert r.status_code == 200, r.text

    body = r.json()
    # Contract requires these exact keys.
    assert body["pipeline_state"] in {"idle", "running", "paused", "error"}
    # ``active_task_id`` may be None when idle but the key must be present.
    assert "active_task_id" in body
    assert body["gate"] in {"open", "closed"}
    assert body["sandbox"] in {"enabled", "disabled", "unknown"}


# ── /api/dispatch ─────────────────────────────────────────────────────────


def test_api_dispatch_rejects_invalid_token(
    smoke_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop],
) -> None:
    """``POST /api/dispatch`` returns 403 when ``confirmation_token`` is wrong."""
    _, client, _ = smoke_system
    r = client.post(
        "/api/dispatch",
        json={
            "confirmation_token": "not-the-token",
            "prompt": "hello",
            "idempotency_key": "phase1-rej-1",
        },
    )
    assert r.status_code == 403, r.text


def test_api_dispatch_accepts_signed_envelope(
    smoke_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop],
) -> None:
    """``POST /api/dispatch`` enqueues and returns the documented response."""
    _, client, _ = smoke_system
    r = client.post(
        "/api/dispatch",
        json={
            "confirmation_token": AUTH_TOKEN,
            "prompt": "phase 1 smoke prompt",
            "idempotency_key": "phase1-disp-1",
        },
    )
    assert r.status_code == 202, r.text

    body = r.json()
    # Contract: DispatchResponse → {task_id, status, queued_at}.
    assert body["task_id"] == "phase1-disp-1"
    assert body["status"] in {"queued", "running", "completed"}
    assert isinstance(body["queued_at"], str) and body["queued_at"]


# ── /api/control/{pause,resume,abort} ─────────────────────────────────────


def test_api_control_lifecycle_round_trip(
    smoke_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop],
) -> None:
    """Pause → resume → abort each return ControlResponse with the requested action."""
    _, client, _ = smoke_system

    for action in ("pause", "resume", "abort"):
        r = client.post(
            f"/api/control/{action}",
            json={"confirmation_token": AUTH_TOKEN, "reason": f"phase1 {action}"},
        )
        assert r.status_code == 200, (action, r.text)
        body = r.json()
        assert body["action"] == action
        assert isinstance(body["applied_at"], str) and body["applied_at"]
        # ``previous_state`` is required by the contract; value is informational.
        assert "previous_state" in body


def test_api_control_rejects_invalid_action(
    smoke_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop],
) -> None:
    """An unknown action returns 422; the path remains stable."""
    _, client, _ = smoke_system
    r = client.post(
        "/api/control/notarealaction",
        json={"confirmation_token": AUTH_TOKEN},
    )
    assert r.status_code == 422, r.text


def test_api_control_rejects_invalid_token(
    smoke_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop],
) -> None:
    """An invalid confirmation token short-circuits with 403."""
    _, client, _ = smoke_system
    r = client.post(
        "/api/control/pause",
        json={"confirmation_token": "wrong", "reason": "phase1"},
    )
    assert r.status_code == 403, r.text


# ── WebSocket smoke (best-effort — see docstring) ─────────────────────────


def test_websocket_events_round_trip(
    smoke_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop],
) -> None:
    """Connect to ``/api/v1/events``, observe at least one frame, disconnect.

    The endpoint streams whatever the in-process EventBus publishes.  We
    publish a synthetic event from the same loop to guarantee determinism
    rather than racing on real daemon activity.

    Note: this repo *does* expose a WebSocket events endpoint, so the
    skip-and-document branch from the brief does not apply.  If a future
    refactor removes ``/api/v1/events``, this test should be replaced with
    a clean ``pytest.skip("no /ws endpoint in this build")``.
    """
    _, client, _ = smoke_system

    # Static-token auth path: ``/api/v1/events`` accepts a static token via
    # the ``token`` query string.  Triggering a real ``/api/dispatch`` while
    # the socket is open guarantees a ``TASK_QUEUED`` frame is published on
    # the same loop that owns the WS subscription, avoiding a flaky
    # cross-loop publish from the test thread.
    with client.websocket_connect(f"/api/v1/events?token={AUTH_TOKEN}") as ws:
        r = client.post(
            "/api/dispatch",
            json={
                "confirmation_token": AUTH_TOKEN,
                "prompt": "ws smoke prompt",
                "idempotency_key": "phase1-ws-1",
            },
        )
        assert r.status_code == 202, r.text

        # Starlette's TestClient WebSocket is synchronous; receive_text
        # blocks until the next frame.  The preceding dispatch guarantees a
        # frame is enqueued, so this should not hang.
        frame = ws.receive_text()
        # Event.to_json() emits {"kind": ..., "ts": ..., "payload": ...}.
        assert "kind" in frame
        assert "payload" in frame
