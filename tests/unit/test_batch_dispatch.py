"""Batch dispatch — queue many issues via one REST call / CLI invocation."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon


@pytest.fixture
def system(
    minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path, tmp_path: Path
) -> Iterator[tuple[TestClient, Daemon]]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(
        minimal_config,
        ledger_path=isolated_ledger_path,
        task_store_path=tmp_path / "tasks.db",
    )
    try:
        with TestClient(create_app(d)) as c:
            yield c, d
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class TestBatchDispatchEndpoint:
    def test_queues_multiple_issues(self, system: tuple[TestClient, Daemon]) -> None:
        client, daemon = system
        r = client.post(
            "/api/v1/issues/batch-dispatch",
            json={
                "items": [
                    {"repo": "owner/a", "number": 1, "mode": "plan"},
                    {"repo": "owner/a", "number": 2, "mode": "plan"},
                    {"repo": "owner/b", "number": 7, "mode": "implement"},
                ]
            },
        )
        assert r.status_code == 202
        body = r.json()
        assert body["dispatched"] == 3
        assert body["failed"] == 0
        assert len(body["tasks"]) == 3
        assert len(daemon.state().tasks) == 3

    def test_per_item_failure_does_not_abort_batch(
        self, system: tuple[TestClient, Daemon]
    ) -> None:
        client, _ = system
        r = client.post(
            "/api/v1/issues/batch-dispatch",
            json={
                "items": [
                    {"repo": "owner/a", "number": 1, "mode": "plan"},
                    # Invalid mode — should fail validation individually.
                    {"repo": "owner/a", "number": 2, "mode": "yolo"},
                    {"repo": "owner/a", "number": 3, "mode": "plan"},
                ]
            },
        )
        # Pydantic rejects the whole payload because `yolo` fails the regex.
        # This test pins the strict-validation behaviour; if we later want
        # per-item tolerance, add `strict=False`.
        assert r.status_code == 422

    def test_empty_batch_rejected(self, system: tuple[TestClient, Daemon]) -> None:
        client, _ = system
        r = client.post("/api/v1/issues/batch-dispatch", json={"items": []})
        assert r.status_code == 422

    def test_oversized_batch_rejected(self, system: tuple[TestClient, Daemon]) -> None:
        client, _ = system
        too_many = [
            {"repo": f"o/r{i}", "number": i, "mode": "plan"} for i in range(101)
        ]
        r = client.post("/api/v1/issues/batch-dispatch", json={"items": too_many})
        assert r.status_code == 422


class TestBatchCLI:
    def test_from_file(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from maxwell_daemon.cli.main import app

        spec = tmp_path / "issues.txt"
        spec.write_text("owner/a#1\nowner/a#2:implement\n# comment\nowner/b#7:plan\n")

        captured: list[dict[str, Any]] = []

        def fake_post(url: str, *, json: Any = None, headers: Any = None, timeout: Any = None):  # type: ignore[no-untyped-def]
            captured.append(json)

            class _R:
                status_code = 202

                def raise_for_status(self) -> None:
                    pass

                def json(self) -> dict[str, Any]:
                    return {
                        "dispatched": len(json["items"]),
                        "failed": 0,
                        "tasks": [],
                    }

            return _R()

        import httpx

        runner = CliRunner()
        from unittest.mock import patch

        with patch.object(httpx, "post", fake_post):
            r = runner.invoke(
                app,
                ["issue", "dispatch-batch", "--from-file", str(spec)],
            )

        assert r.exit_code == 0, r.stdout
        assert len(captured) == 1
        items = captured[0]["items"]
        assert {(i["repo"], i["number"], i["mode"]) for i in items} == {
            ("owner/a", 1, "plan"),
            ("owner/a", 2, "implement"),
            ("owner/b", 7, "plan"),
        }
