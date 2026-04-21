"""Tests for SSH key store and session pool.

asyncssh is optional — tests are skipped when it is not installed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

asyncssh = pytest.importorskip("asyncssh")

from maxwell_daemon.ssh.keys import SSHKeyStore  # noqa: E402
from maxwell_daemon.ssh.session import (  # noqa: E402
    SSHSession,
    SSHSessionPool,
)

# ── SSHKeyStore ──────────────────────────────────────────────────────────────


class TestSSHKeyStore:
    def test_get_or_generate_creates_files(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        _priv, pub = store.get_or_generate("testhost")
        assert (tmp_path / "testhost.pem").is_file()
        assert (tmp_path / "testhost.pub").is_file()
        assert pub.startswith("ssh-")

    def test_get_or_generate_idempotent(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        _, pub1 = store.get_or_generate("testhost")
        _, pub2 = store.get_or_generate("testhost")
        assert pub1 == pub2

    def test_pem_has_restricted_permissions(self, tmp_path: Path) -> None:
        import stat

        store = SSHKeyStore(tmp_path)
        store.get_or_generate("testhost")
        mode = (tmp_path / "testhost.pem").stat().st_mode
        assert not (mode & stat.S_IRGRP)
        assert not (mode & stat.S_IROTH)

    def test_list_machines(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        store.get_or_generate("alpha")
        store.get_or_generate("beta")
        assert store.list_machines() == ["alpha", "beta"]

    def test_list_machines_empty_dir(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path / "nodir")
        assert store.list_machines() == []

    def test_remove_deletes_files(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        store.get_or_generate("testhost")
        store.remove("testhost")
        assert not (tmp_path / "testhost.pem").exists()
        assert not (tmp_path / "testhost.pub").exists()

    def test_remove_nonexistent_is_noop(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        store.remove("ghost")  # must not raise

    def test_public_key_string_none_when_absent(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        assert store.public_key_string("nobody") is None

    def test_public_key_string_returns_key(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        _, pub = store.get_or_generate("srv")
        assert store.public_key_string("srv") == pub


# ── SSHSession ───────────────────────────────────────────────────────────────


def _make_session() -> SSHSession:
    conn = MagicMock()
    conn.run = AsyncMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock()
    import time

    return SSHSession(conn, time.monotonic())


class TestSSHSession:
    @pytest.mark.asyncio
    async def test_run_records_history(self) -> None:
        session = _make_session()
        mock_result = MagicMock()
        mock_result.stdout = "hello"
        mock_result.stderr = ""
        mock_result.exit_status = 0
        session._conn.run = AsyncMock(return_value=mock_result)

        result = await session.run("echo hello")
        assert result.stdout == "hello"
        assert result.exit_code == 0
        assert "echo hello" in session.command_history()

    @pytest.mark.asyncio
    async def test_close_calls_conn_close(self) -> None:
        session = _make_session()
        await session.close()
        session._conn.close.assert_called_once()
        session._conn.wait_closed.assert_awaited_once()

    def test_is_expired_false_for_fresh_session(self) -> None:
        session = _make_session()
        assert not session.is_expired


# ── SSHSessionPool ───────────────────────────────────────────────────────────


class TestSSHSessionPool:
    @pytest.mark.asyncio
    async def test_get_creates_session(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        pool = SSHSessionPool(store, known_hosts=None)

        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()

        with patch("asyncssh.connect", AsyncMock(return_value=mock_conn)):
            session = await pool.get("myhost", user="ubuntu")
        assert session is not None

    @pytest.mark.asyncio
    async def test_get_reuses_session(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        pool = SSHSessionPool(store, known_hosts=None)

        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()

        with patch("asyncssh.connect", AsyncMock(return_value=mock_conn)) as mock_connect:
            s1 = await pool.get("myhost", user="ubuntu")
            s2 = await pool.get("myhost", user="ubuntu")
        assert s1 is s2
        assert mock_connect.call_count == 1  # only one real connection

    @pytest.mark.asyncio
    async def test_sessions_summary(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        pool = SSHSessionPool(store, known_hosts=None)

        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()

        with patch("asyncssh.connect", AsyncMock(return_value=mock_conn)):
            await pool.get("srv1", user="root")
        summaries = pool.sessions()
        assert len(summaries) == 1
        assert summaries[0]["host"] == "srv1"

    @pytest.mark.asyncio
    async def test_close_all(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        pool = SSHSessionPool(store, known_hosts=None)

        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()

        with patch("asyncssh.connect", AsyncMock(return_value=mock_conn)):
            await pool.get("srv1", user="root")
        await pool.close_all()
        assert pool.sessions() == []
