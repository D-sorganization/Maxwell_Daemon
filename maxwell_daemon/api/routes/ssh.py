"""SSH key store, session pool, and WebSocket shell endpoints.

asyncssh is optional (``pip install maxwell-daemon[ssh]``). All SSH routes
return HTTP 503 if it is not installed.

Extracted from ``maxwell_daemon/api/server.py`` as part of epic #896
Phase 1.1.
"""

from __future__ import annotations

import json as _json_mod
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel

from maxwell_daemon.audit import AuditLogger
from maxwell_daemon.auth import JWTConfig, Role
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "SSHConnectRequest",
    "SSHRunRequest",
    "register",
]


def make_require_auth_configured(auth_token: str | None, jwt_config: JWTConfig | None) -> Any:
    """Return a dependency that fails *closed* when no auth is configured.

    SSH endpoints expose remote command execution against any host with a
    stored/agent key. Unlike read-only routes, they must never run in the
    fully-open dev mode that ``make_rbac_dep`` falls back to when neither a
    static ``auth_token`` nor a ``jwt_config`` is set (issue #965). This guard
    mirrors the safe-closed contract of ``POST /api/dispatch``: with no auth
    configured the request is rejected before any side effect, returning
    ``503 Service Unavailable`` (the server is misconfigured for this surface)
    rather than silently admitting an unauthenticated caller.
    """

    async def _dep() -> None:
        if auth_token is None and jwt_config is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "SSH endpoints are disabled: configure api.auth_token or "
                    "api.jwt_secret to enable authenticated SSH access."
                ),
            )

    return _dep


_SSH_ALLOWED_COMMANDS: frozenset[str] = frozenset({"bash", "sh", "zsh", "fish", "rbash"})


class SSHConnectRequest(BaseModel):
    host: str
    port: int = 22
    user: str
    password: str | None = None


class SSHRunRequest(BaseModel):
    host: str
    port: int = 22
    user: str
    command: str
    timeout_seconds: float = 30.0


def register(  # noqa: C901
    app: FastAPI,
    daemon: Daemon,
    auth_token: str | None,
    jwt_config: JWTConfig | None,
    audit: AuditLogger | None,
    require_admin: Any,
    auth: Any,
    websocket_auth_or_close: Any,
) -> None:
    """Attach SSH endpoints to ``app``."""

    # Safe-closed guard: SSH (remote command execution) must reject every
    # request when no auth is configured, instead of falling through to the
    # open dev mode that ``require_admin`` permits (issue #965).
    require_auth_configured = make_require_auth_configured(auth_token, jwt_config)

    _ssh_pool_ref: dict[str, Any] = {}

    def _ssh_pool() -> Any:
        if "pool" not in _ssh_pool_ref:
            try:
                import asyncssh as _asyncssh  # noqa: F401 — presence check only

                from maxwell_daemon.ssh.session import SSHSessionPool

                _ssh_pool_ref["pool"] = SSHSessionPool()
            except ImportError:
                _ssh_pool_ref["pool"] = None
        return _ssh_pool_ref.get("pool")

    def _ssh_unavailable() -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"detail": "SSH support not installed — pip install maxwell-daemon[ssh]"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.get(
        "/api/v1/ssh/sessions",
        dependencies=[Depends(require_auth_configured), Depends(require_admin)],
    )
    async def ssh_sessions() -> Any:
        """List active SSH sessions."""
        pool = _ssh_pool()
        if pool is None:
            return _ssh_unavailable()
        return {"sessions": pool.sessions()}

    @app.get(
        "/api/v1/ssh/keys",
        dependencies=[Depends(require_auth_configured), Depends(require_admin)],
    )
    async def ssh_list_keys() -> Any:
        """List machines that have stored SSH keys."""
        try:
            from maxwell_daemon.ssh.keys import SSHKeyStore
        except ImportError:
            return _ssh_unavailable()
        store = SSHKeyStore()
        return {"machines": store.list_machines()}

    @app.get(
        "/api/v1/ssh/keys/{machine}",
        dependencies=[Depends(require_auth_configured), Depends(require_admin)],
    )
    async def ssh_get_key(machine: str) -> Any:
        """Return the public key for *machine*, generating it if absent."""
        try:
            from maxwell_daemon.ssh.keys import SSHKeyStore
        except ImportError:
            return _ssh_unavailable()
        store = SSHKeyStore()
        _, pub = store.get_or_generate(machine)
        return {"machine": machine, "public_key": pub}

    @app.delete(
        "/api/v1/ssh/keys/{machine}",
        dependencies=[Depends(require_auth_configured), Depends(require_admin)],
    )
    async def ssh_delete_key(machine: str) -> Any:
        """Remove stored SSH keys for *machine*."""
        try:
            from maxwell_daemon.ssh.keys import SSHKeyStore
        except ImportError:
            return _ssh_unavailable()
        SSHKeyStore().remove(machine)
        return {"machine": machine, "deleted": True}

    @app.post(
        "/api/v1/ssh/connect",
        dependencies=[Depends(require_auth_configured), Depends(auth), Depends(require_admin)],
    )
    async def ssh_connect(payload: SSHConnectRequest) -> Any:
        """Open (or reuse) an SSH session and return its summary."""
        pool = _ssh_pool()
        if pool is None:
            return _ssh_unavailable()
        session = await pool.get(
            payload.host,
            user=payload.user,
            port=payload.port,
            password=payload.password,
        )
        return {
            "host": payload.host,
            "port": payload.port,
            "user": payload.user,
            "age_seconds": round(session.age_seconds, 1),
        }

    @app.post(
        "/api/v1/ssh/run",
        dependencies=[Depends(require_auth_configured), Depends(auth), Depends(require_admin)],
    )
    async def ssh_run(payload: SSHRunRequest) -> Any:
        """Run a command on a remote machine and return its output."""
        pool = _ssh_pool()
        if pool is None:
            return _ssh_unavailable()
        session = await pool.get(payload.host, user=payload.user, port=payload.port)
        result = await session.run(payload.command, timeout=payload.timeout_seconds)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }

    @app.get(
        "/api/v1/ssh/files",
        dependencies=[Depends(require_auth_configured), Depends(require_admin)],
    )
    async def ssh_list_files(
        host: str = Query(...),
        user: str = Query(...),
        port: int = Query(default=22),
        path: str = Query(default="/"),
    ) -> Any:
        """List files on a remote machine via SFTP."""
        pool = _ssh_pool()
        if pool is None:
            return _ssh_unavailable()
        session = await pool.get(host, user=user, port=port)
        entries = await session.list_dir(path)
        return {
            "path": path,
            "entries": [
                {
                    "name": e.name,
                    "path": e.path,
                    "size": e.size,
                    "is_dir": e.is_dir,
                    "modified": e.modified,
                }
                for e in entries
            ],
        }

    @app.websocket("/api/v1/ssh/shell")
    async def ssh_shell_ws(ws: WebSocket) -> None:
        """Interactive shell over WebSocket.

        Query params: ``host``, ``user``, ``port`` (default 22), ``token``
        (bearer token for auth), ``command`` (default ``bash``).

        The ``command`` parameter is validated against an explicit whitelist of
        permitted shell executables.  Arbitrary shell strings, pipes, and
        redirections are rejected to prevent remote code execution via command
        injection (CVE / Issue #138).

        Frames: text frames sent from client are written to stdin.
        Text frames sent to client contain stdout/stderr chunks.
        Session ends when the command exits or the client disconnects.
        Max session duration: 1 hour.
        """
        # Safe-closed: refuse the interactive shell entirely when no auth is
        # configured, rather than admitting via open dev mode (issue #965).
        if auth_token is None and jwt_config is None:
            await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="SSH auth not configured")
            return
        if not await websocket_auth_or_close(
            ws,
            Role.admin,
            auth_token,
            jwt_config,
            getattr(daemon, "_auth_store", None),
            audit,
        ):
            return

        pool = _ssh_pool()
        if pool is None:
            await ws.accept()
            await ws.send_text('{"error": "SSH not installed"}')
            await ws.close(code=1011)
            return

        host = ws.query_params.get("host") or ""
        user = ws.query_params.get("user") or ""

        raw_port = ws.query_params.get("port") or "22"
        try:
            port = int(raw_port)
            if not (1 <= port <= 65535):
                raise ValueError("port out of range")
        except ValueError:
            await ws.accept()
            await ws.send_text('{"error": "invalid port"}')
            await ws.close(code=1008)
            return

        raw_command = ws.query_params.get("command") or "bash"
        command = raw_command.strip()
        if command not in _SSH_ALLOWED_COMMANDS:
            await ws.accept()
            await ws.send_text(
                _json_mod.dumps(
                    {
                        "error": (
                            f"command {command!r} is not permitted; "
                            f"allowed: {sorted(_SSH_ALLOWED_COMMANDS)}"
                        )
                    }
                )
            )
            await ws.close(code=1008)
            return

        if not host or not user:
            await ws.accept()
            await ws.send_text('{"error": "host and user are required"}')
            await ws.close(code=1008)
            return

        await ws.accept()
        try:
            session = await pool.get(host, user=user, port=port)
            async for chunk in session.shell_stream(command):
                await ws.send_bytes(chunk)
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001
            await ws.send_text(_json_mod.dumps({"error": str(exc)}))
            await ws.close(code=1011)
