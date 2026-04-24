"""Tests for SSH key store and session pool.

asyncssh is optional — tests are skipped when it is not installed.
"""

from __future__ import annotations

import asyncio
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

    @pytest.mark.skipif(
        __import__("sys").platform == "win32",
        reason="Windows does not enforce Unix-style POSIX file permissions",
    )
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
    def test_run_records_history(self) -> None:
        session = _make_session()
        mock_result = MagicMock()
        mock_result.stdout = "hello"
        mock_result.stderr = ""
        mock_result.exit_status = 0
        session._conn.run = AsyncMock(return_value=mock_result)

        result = asyncio.run(session.run("echo hello"))
        assert result.stdout == "hello"
        assert result.exit_code == 0
        assert "echo hello" in session.command_history()

    def test_close_calls_conn_close(self) -> None:
        session = _make_session()
        asyncio.run(session.close())
        session._conn.close.assert_called_once()
        session._conn.wait_closed.assert_awaited_once()

    def test_is_expired_false_for_fresh_session(self) -> None:
        session = _make_session()
        assert not session.is_expired

    def test_is_expired_true_for_old_session(self) -> None:
        import time

        from maxwell_daemon.ssh.session import SESSION_TTL_SECONDS

        session = SSHSession(_make_session()._conn, time.monotonic() - SESSION_TTL_SECONDS - 1)
        assert session.is_expired

    def test_shell_stream_yields_chunks(self) -> None:
        async def _run() -> list[bytes]:
            session = _make_session()

            async def _fake_chunks():
                yield "hello "
                yield b"world"

            mock_process = MagicMock()
            mock_process.stdout = _fake_chunks()
            mock_process.close = MagicMock()
            mock_process.wait_closed = AsyncMock()
            session._conn.create_process = AsyncMock(return_value=mock_process)

            chunks = []
            async for chunk in session.shell_stream("bash"):
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(_run())
        assert b"hello " in chunks
        assert b"world" in chunks

    def test_write_stdin(self) -> None:
        session = _make_session()
        mock_process = MagicMock()
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.drain = AsyncMock()
        asyncio.run(session.write_stdin(mock_process, b"input"))
        mock_process.stdin.write.assert_called_once_with(b"input")

    def test_list_dir_returns_entries(self) -> None:
        async def _run() -> list:
            session = _make_session()
            mock_sftp = MagicMock()

            async def _scandir(path: str):
                entry = MagicMock()
                entry.filename = "file.txt"
                entry.attrs.size = 100
                entry.attrs.permissions = 0o100644  # regular file
                entry.attrs.mtime = 1700000000
                yield entry

            mock_sftp.scandir = _scandir
            mock_sftp.__aenter__ = AsyncMock(return_value=mock_sftp)
            mock_sftp.__aexit__ = AsyncMock(return_value=False)
            session._conn.start_sftp_client = MagicMock(return_value=mock_sftp)

            return await session.list_dir("/home")

        entries = asyncio.run(_run())
        assert len(entries) == 1
        assert entries[0].name == "file.txt"
        assert entries[0].size == 100
        assert not entries[0].is_dir

    def test_download_returns_bytes(self) -> None:
        async def _run() -> bytes:
            session = _make_session()
            mock_fh = MagicMock()
            mock_fh.read = AsyncMock(return_value=b"file content")
            mock_fh.__aenter__ = AsyncMock(return_value=mock_fh)
            mock_fh.__aexit__ = AsyncMock(return_value=False)
            mock_sftp = MagicMock()
            mock_sftp.open = MagicMock(return_value=mock_fh)
            mock_sftp.__aenter__ = AsyncMock(return_value=mock_sftp)
            mock_sftp.__aexit__ = AsyncMock(return_value=False)
            session._conn.start_sftp_client = MagicMock(return_value=mock_sftp)
            return await session.download("/remote/file.txt")

        data = asyncio.run(_run())
        assert data == b"file content"


# ── SSHSessionPool ───────────────────────────────────────────────────────────


class TestSSHSessionPool:
    def test_get_creates_session(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        pool = SSHSessionPool(store, known_hosts=None)

        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()

        with patch("asyncssh.connect", AsyncMock(return_value=mock_conn)):
            session = asyncio.run(pool.get("myhost", user="ubuntu"))
        assert session is not None

    def test_get_reuses_session(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        pool = SSHSessionPool(store, known_hosts=None)

        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()

        async def _run() -> tuple[SSHSession, SSHSession]:
            with patch("asyncssh.connect", AsyncMock(return_value=mock_conn)) as mock_connect:
                s1 = await pool.get("myhost", user="ubuntu")
                s2 = await pool.get("myhost", user="ubuntu")
                assert mock_connect.call_count == 1
            return s1, s2

        s1, s2 = asyncio.run(_run())
        assert s1 is s2

    def test_sessions_summary(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        pool = SSHSessionPool(store, known_hosts=None)

        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()

        async def _run() -> None:
            with patch("asyncssh.connect", AsyncMock(return_value=mock_conn)):
                await pool.get("srv1", user="root")

        asyncio.run(_run())
        summaries = pool.sessions()
        assert len(summaries) == 1
        assert summaries[0]["host"] == "srv1"

    def test_close_all(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        pool = SSHSessionPool(store, known_hosts=None)

        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()

        async def _run() -> None:
            with patch("asyncssh.connect", AsyncMock(return_value=mock_conn)):
                await pool.get("srv1", user="root")
            await pool.close_all()

        asyncio.run(_run())
        assert pool.sessions() == []

    def test_get_with_password(self, tmp_path: Path) -> None:
        store = SSHKeyStore(tmp_path)
        pool = SSHSessionPool(store, known_hosts=None)

        mock_conn = MagicMock()
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()

        async def _run() -> None:
            with patch("asyncssh.connect", AsyncMock(return_value=mock_conn)) as mock_connect:
                await pool.get("myhost", user="ubuntu", password="secret")
                call_kwargs = mock_connect.call_args[1]
                assert call_kwargs["password"] == "secret"
                assert "client_keys" not in call_kwargs

        asyncio.run(_run())

    def test_get_replaces_expired_session(self, tmp_path: Path) -> None:
        import time

        from maxwell_daemon.ssh.session import SESSION_TTL_SECONDS

        store = SSHKeyStore(tmp_path)
        pool = SSHSessionPool(store, known_hosts=None)

        mock_conn1 = MagicMock()
        mock_conn1.close = MagicMock()
        mock_conn1.wait_closed = AsyncMock()
        mock_conn2 = MagicMock()
        mock_conn2.close = MagicMock()
        mock_conn2.wait_closed = AsyncMock()

        async def _run() -> None:
            with patch("asyncssh.connect", AsyncMock(return_value=mock_conn1)):
                s1 = await pool.get("host", user="u")
            # Artificially age the session past TTL
            s1.created_at = time.monotonic() - SESSION_TTL_SECONDS - 1
            with patch("asyncssh.connect", AsyncMock(return_value=mock_conn2)):
                s2 = await pool.get("host", user="u")
            assert s1 is not s2
            mock_conn1.close.assert_called_once()

        asyncio.run(_run())
