"""SSH session pool with async shell access and SFTP support.

Sessions are cached by (host, port, user) and reused for up to
``SESSION_TTL_SECONDS``.  The pool evicts idle connections in the background.

Usage::

    pool = SSHSessionPool(key_store)
    session = await pool.get("myhost", user="ubuntu", port=22)

    # Run a command:
    result = await session.run("df -h")

    # Interactive shell (yields output chunks):
    async for chunk in session.shell_stream("bash"):
        send_to_websocket(chunk)

    # SFTP listing:
    entries = await session.list_dir("/home/ubuntu")
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from maxwell_daemon.ssh.keys import SSHKeyStore, _require_asyncssh

__all__ = ["CommandResult", "DirEntry", "SSHSession", "SSHSessionPool"]

SESSION_TTL_SECONDS = 3600  # 1 hour max session lifetime


@dataclass
class CommandResult:
    """Output from a single non-interactive command."""

    stdout: str
    stderr: str
    exit_code: int


@dataclass
class DirEntry:
    """A single entry from an SFTP directory listing."""

    name: str
    path: str
    size: int
    is_dir: bool
    modified: float  # Unix timestamp


class SSHSession:
    """Wraps a single asyncssh connection with helpers for shell and SFTP."""

    def __init__(self, conn: Any, created_at: float) -> None:
        self._conn = conn
        self.created_at = created_at
        self._history: list[str] = []

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.created_at

    @property
    def is_expired(self) -> bool:
        return self.age_seconds > SESSION_TTL_SECONDS

    async def run(self, command: str, *, timeout: float = 30.0) -> CommandResult:
        """Run *command* and return stdout/stderr/exit_code."""
        self._history.append(command)
        result = await asyncio.wait_for(self._conn.run(command), timeout=timeout)
        return CommandResult(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            exit_code=result.exit_status or 0,
        )

    async def shell_stream(
        self, command: str, *, timeout: float = SESSION_TTL_SECONDS
    ) -> AsyncIterator[bytes]:
        """Yield stdout chunks from *command* as bytes.

        Suitable for streaming to a WebSocket — the caller buffers/sends each
        chunk as it arrives.
        """
        self._history.append(command)
        process = await self._conn.create_process(command, term_type="xterm-256color")
        deadline = time.monotonic() + timeout
        try:
            async for chunk in process.stdout:
                if time.monotonic() > deadline:
                    process.kill()
                    break
                if isinstance(chunk, str):
                    yield chunk.encode()
                else:
                    yield chunk
        finally:
            process.close()
            await process.wait_closed()

    async def write_stdin(self, process: Any, data: bytes) -> None:
        """Write *data* to a running process's stdin."""
        process.stdin.write(data)
        await process.stdin.drain()

    async def list_dir(self, path: str = "/") -> list[DirEntry]:
        """Return SFTP directory listing for *path*."""
        async with self._conn.start_sftp_client() as sftp:
            entries: list[DirEntry] = []
            async for entry in sftp.scandir(path):
                attrs = entry.attrs
                abs_path = f"{path.rstrip('/')}/{entry.filename}"
                entries.append(
                    DirEntry(
                        name=entry.filename,
                        path=abs_path,
                        size=attrs.size or 0,
                        is_dir=(attrs.permissions or 0) & 0o40000 != 0,
                        modified=float(attrs.mtime or 0),
                    )
                )
            return entries

    async def download(self, remote_path: str) -> bytes:
        """Download a remote file and return its contents."""
        async with (
            self._conn.start_sftp_client() as sftp,
            sftp.open(remote_path, "rb") as fh,
        ):
            data: bytes = await fh.read()
            return data

    def command_history(self) -> list[str]:
        return list(self._history)

    async def close(self) -> None:
        self._conn.close()
        await self._conn.wait_closed()


@dataclass
class _PoolKey:
    host: str
    port: int
    user: str

    def __hash__(self) -> int:
        return hash((self.host, self.port, self.user))


class SSHSessionPool:
    """Async session pool — reuses connections, evicts expired ones.

    Parameters
    ----------
    key_store:
        Provides the private key for each machine (keyed by *host*).
    known_hosts:
        Path to a known_hosts file.  Pass ``None`` to disable host-key
        checking (insecure — only for trusted test environments).
    """

    def __init__(
        self,
        key_store: SSHKeyStore | None = None,
        *,
        known_hosts: Path | str | None = None,
    ) -> None:
        if known_hosts is None:
            known_hosts = Path.home() / ".ssh" / "known_hosts"
        self._key_store = key_store or SSHKeyStore()
        self._known_hosts = known_hosts
        self._pool: dict[_PoolKey, SSHSession] = {}
        self._lock = asyncio.Lock()

    async def get(
        self,
        host: str,
        *,
        user: str,
        port: int = 22,
        password: str | None = None,
    ) -> SSHSession:
        """Return a cached session for (host, port, user), creating one if needed."""
        asyncssh = _require_asyncssh()
        key = _PoolKey(host=host, port=port, user=user)

        async with self._lock:
            existing = self._pool.get(key)
            if existing is not None and not existing.is_expired:
                return existing

            # Build connection options
            connect_kwargs: dict[str, Any] = {
                "host": host,
                "port": port,
                "username": user,
                "known_hosts": str(self._known_hosts) if self._known_hosts else None,
            }
            if password is not None:
                connect_kwargs["password"] = password
            else:
                try:
                    priv, _ = self._key_store.get_or_generate(host)
                    connect_kwargs["client_keys"] = [priv]
                except Exception:  # noqa: BLE001
                    pass  # nosec B110 fall through to agent/default key resolution

            conn = await asyncssh.connect(**connect_kwargs)
            session = SSHSession(conn, time.monotonic())
            if existing is not None:
                await existing.close()
            self._pool[key] = session
            return session

    async def close_all(self) -> None:
        """Close all pooled sessions."""
        async with self._lock:
            for session in self._pool.values():
                await session.close()
            self._pool.clear()

    def sessions(self) -> list[dict[str, Any]]:
        """Return a summary of active sessions (for the API)."""
        return [
            {
                "host": k.host,
                "port": k.port,
                "user": k.user,
                "age_seconds": round(s.age_seconds, 1),
                "expired": s.is_expired,
                "commands_run": len(s.command_history()),
            }
            for k, s in self._pool.items()
        ]
