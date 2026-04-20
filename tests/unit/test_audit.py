"""Tests for the AuditLogger and verify_chain."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from maxwell_daemon.audit import AuditLogger, verify_chain

try:
    import prometheus_client  # noqa: F401

    _HAS_API_DEPS = True
except ModuleNotFoundError:
    _HAS_API_DEPS = False


@pytest.fixture
def daemon(minimal_config: Any, isolated_ledger_path: Any) -> Iterator[Any]:
    if not _HAS_API_DEPS:
        pytest.skip("api deps not installed")
    from maxwell_daemon.daemon import Daemon

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
    loop.run_until_complete(d.start(worker_count=1))
    try:
        yield d
    finally:
        loop.run_until_complete(d.stop())
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def client(daemon: Any) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from maxwell_daemon.api import create_app

    with TestClient(create_app(daemon)) as c:
        yield c


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def logger(log_path: Path) -> AuditLogger:
    return AuditLogger(log_path)


class TestAuditLogger:
    def test_creates_file_on_first_write(self, logger: AuditLogger, log_path: Path) -> None:
        logger.log_api_call(method="GET", path="/health", status=200)
        assert log_path.is_file()

    def test_entry_fields_present(self, logger: AuditLogger, log_path: Path) -> None:
        logger.log_api_call(method="POST", path="/api/v1/tasks", status=202, request_id="abc")
        lines = log_path.read_text().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["event_type"] == "api_call"
        assert obj["method"] == "POST"
        assert obj["path"] == "/api/v1/tasks"
        assert obj["status"] == 202
        assert obj["request_id"] == "abc"
        assert "timestamp" in obj
        assert "entry_hash" in obj
        assert "prev_hash" in obj

    def test_first_entry_genesis_prev_hash(self, logger: AuditLogger, log_path: Path) -> None:
        logger.log_api_call(method="GET", path="/health", status=200)
        obj = json.loads(log_path.read_text())
        assert obj["prev_hash"] == "0" * 64

    def test_chain_links_entries(self, logger: AuditLogger, log_path: Path) -> None:
        logger.log_api_call(method="GET", path="/health", status=200)
        logger.log_api_call(method="POST", path="/api/v1/tasks", status=202)
        lines = log_path.read_text().splitlines()
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert second["prev_hash"] == first["entry_hash"]

    def test_multiple_entries_appended(self, logger: AuditLogger, log_path: Path) -> None:
        for i in range(5):
            logger.log_api_call(method="GET", path=f"/api/v1/tasks/{i}", status=200)
        assert len(log_path.read_text().splitlines()) == 5

    def test_log_agent_operation(self, logger: AuditLogger, log_path: Path) -> None:
        logger.log_agent_operation(operation="task_start", task_id="t-1", repo="org/repo")
        obj = json.loads(log_path.read_text())
        assert obj["event_type"] == "agent_operation"
        assert obj["details"]["operation"] == "task_start"
        assert obj["details"]["task_id"] == "t-1"

    def test_log_config_change(self, logger: AuditLogger, log_path: Path) -> None:
        logger.log_config_change(key="api.auth_token", user="admin")
        obj = json.loads(log_path.read_text())
        assert obj["event_type"] == "config_change"
        assert obj["user"] == "admin"
        assert obj["details"]["key"] == "api.auth_token"

    def test_entries_pagination(self, logger: AuditLogger) -> None:
        for i in range(10):
            logger.log_api_call(method="GET", path=f"/{i}", status=200)
        page = logger.entries(limit=3, offset=2)
        assert len(page) == 3

    def test_entries_empty_when_no_file(self, log_path: Path) -> None:
        fresh = AuditLogger(log_path)
        assert fresh.entries() == []

    def test_rotate_removes_old_entries(self, tmp_path: Path) -> None:
        from datetime import datetime, timedelta, timezone

        path = tmp_path / "audit.jsonl"
        logger = AuditLogger(path, retention_days=7)
        # Write an entry with a timestamp 10 days ago by patching the file directly.
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        # Use a fresh logger to write a "now" entry first, then manually append old one.
        entry = logger.log_api_call(method="GET", path="/old", status=200)
        # Overwrite the file with a backdated entry.
        old_obj = {
            "timestamp": old_ts,
            "event_type": "api_call",
            "method": "GET",
            "path": "/old",
            "status": 200,
            "user": None,
            "request_id": None,
            "details": {},
            "prev_hash": "0" * 64,
            "entry_hash": entry.entry_hash,
        }
        path.write_text(json.dumps(old_obj) + "\n")
        logger._last_hash = None  # reset cache
        # Write a current entry.
        logger.log_api_call(method="GET", path="/new", status=200)
        removed = logger.rotate()
        assert removed == 1
        remaining = logger.entries()
        assert len(remaining) == 1
        assert remaining[0]["path"] == "/new"

    def test_new_logger_reads_tail_from_file(self, log_path: Path) -> None:
        """A fresh AuditLogger instance reads the existing tail hash."""
        logger1 = AuditLogger(log_path)
        e = logger1.log_api_call(method="GET", path="/a", status=200)
        logger2 = AuditLogger(log_path)
        logger2.log_api_call(method="GET", path="/b", status=200)
        lines = log_path.read_text().splitlines()
        second = json.loads(lines[1])
        assert second["prev_hash"] == e.entry_hash


class TestVerifyChain:
    def test_clean_chain(self, logger: AuditLogger, log_path: Path) -> None:
        for i in range(5):
            logger.log_api_call(method="GET", path=f"/{i}", status=200)
        assert verify_chain(log_path) == []

    def test_tampered_entry_detected(self, logger: AuditLogger, log_path: Path) -> None:
        logger.log_api_call(method="GET", path="/a", status=200)
        logger.log_api_call(method="GET", path="/b", status=200)
        lines = log_path.read_text().splitlines()
        obj = json.loads(lines[0])
        obj["status"] = 999  # tamper
        lines[0] = json.dumps(obj)
        log_path.write_text("\n".join(lines) + "\n")
        violations = verify_chain(log_path)
        assert any(v["line"] == 1 for v in violations)

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert verify_chain(tmp_path / "nonexistent.jsonl") == []

    def test_broken_chain_detected(self, logger: AuditLogger, log_path: Path) -> None:
        logger.log_api_call(method="GET", path="/a", status=200)
        logger.log_api_call(method="GET", path="/b", status=200)
        lines = log_path.read_text().splitlines()
        obj = json.loads(lines[1])
        obj["prev_hash"] = "dead" * 16  # break chain
        lines[1] = json.dumps(obj)
        log_path.write_text("\n".join(lines) + "\n")
        violations = verify_chain(log_path)
        assert any("chain broken" in v["error"] for v in violations)


class TestAuditApiEndpoints:
    """Integration tests for /api/v1/audit and /api/v1/audit/verify."""

    def test_audit_disabled_by_default(
        self,
        daemon: Any,
        client: Any,
    ) -> None:
        r = client.get("/api/v1/audit")
        assert r.status_code == 200
        body = r.json()
        assert body["audit_enabled"] is False
        assert body["entries"] == []

    def test_audit_endpoint_with_log(
        self,
        daemon: Any,
        tmp_path: Path,
    ) -> None:
        from fastapi.testclient import TestClient

        from maxwell_daemon.api import create_app

        log_path = tmp_path / "audit.jsonl"
        with TestClient(create_app(daemon, audit_log_path=log_path)) as client:
            client.get("/health")
            r = client.get("/api/v1/audit")
            assert r.status_code == 200
            body = r.json()
            assert body["audit_enabled"] is True
            assert len(body["entries"]) >= 1

            rv = client.get("/api/v1/audit/verify")
            assert rv.status_code == 200
            vbody = rv.json()
            assert vbody["clean"] is True
            assert vbody["violations"] == []
