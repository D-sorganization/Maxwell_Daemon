"""Phase-1 pipeline-lifecycle integration test.

Drives a single task end-to-end through the public surface:

    submit (HTTP) → queued → running → completed → cost ledger row written

The cost ledger is read back via the public ``GET /api/v1/cost`` endpoint
to keep the test honest about the contract — no private SQL pokes, no
direct ledger.records() calls.  This matches the project hard rule from
``CLAUDE.md`` about treating the SQLite cost ledger as append-only audit
state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon


@pytest.fixture
def lifecycle_system(
    tmp_path: Path, register_recording_backend: None
) -> Iterator[tuple[Daemon, TestClient, asyncio.AbstractEventLoop]]:
    """Boot a Daemon + FastAPI app with the stub recording backend."""

    cfg = MaxwellDaemonConfig.model_validate(
        {
            "backends": {
                "primary": {"type": "recording", "model": "lifecycle-model"},
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
    loop.run_until_complete(daemon.start(worker_count=2))
    loop.run_until_complete(asyncio.sleep(0))

    with TestClient(create_app(daemon)) as client:
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


def _wait_for(
    client: TestClient,
    loop: asyncio.AbstractEventLoop,
    task_id: str,
    *,
    timeout_s: float = 30.0,
) -> dict[str, object]:
    """Poll the public detail endpoint while yielding to the daemon's loop.

    Returns the final task envelope once the task hits a terminal state.
    """
    deadline = loop.time() + timeout_s
    last: dict[str, object] = {}
    while loop.time() < deadline:
        last = client.get(f"/api/v1/tasks/{task_id}").json()
        if last.get("status") in {"completed", "failed"}:
            return last
        loop.run_until_complete(asyncio.sleep(0.25))
    raise AssertionError(f"task did not complete within {timeout_s}s: {last}")


class TestPipelineLifecycle:
    def test_task_progresses_queued_running_completed_and_writes_ledger(
        self,
        lifecycle_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop],
    ) -> None:
        """The full happy path, observed via the public HTTP contract."""
        _, client, loop = lifecycle_system

        # ── Submit ──
        submit = client.post("/api/v1/tasks", json={"prompt": "lifecycle smoke"})
        assert submit.status_code == 202, submit.text
        task_id = submit.json()["id"]
        # On submit the task is either ``queued`` or already ``running`` —
        # both are valid transitions; what matters is that we observed a
        # pre-terminal state immediately after submit.
        assert submit.json()["status"] in {"queued", "running"}

        # ── Drive to terminal state ──
        final = _wait_for(client, loop, task_id)
        assert final["status"] == "completed", final

        # ── Cost ledger has a row attributed to the configured backend ──
        cost = client.get("/api/v1/cost").json()
        assert cost["month_to_date_usd"] > 0.0, cost
        # ``primary`` is the alias declared in the test config.
        assert "primary" in cost["by_backend"], cost
        assert cost["by_backend"]["primary"] > 0.0

    def test_status_endpoint_reflects_completed_pipeline(
        self,
        lifecycle_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop],
    ) -> None:
        """After completion ``/api/status`` reports ``idle`` with no active task."""
        _, client, loop = lifecycle_system

        submit = client.post("/api/v1/tasks", json={"prompt": "status post-complete"})
        assert submit.status_code == 202, submit.text
        _wait_for(client, loop, submit.json()["id"])

        # Allow any trailing housekeeping to settle.
        loop.run_until_complete(asyncio.sleep(0.1))

        status = client.get("/api/status").json()
        assert status["pipeline_state"] == "idle", status
        assert status["active_task_id"] is None, status
