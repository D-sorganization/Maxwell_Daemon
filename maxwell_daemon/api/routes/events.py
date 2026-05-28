"""WebSocket events stream endpoint.

Extracted from ``maxwell_daemon/api/server.py`` as part of epic #896 Phase 1.1.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from maxwell_daemon.auth import JWTConfig, Role
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = ["register"]


def register(
    app: FastAPI,
    daemon: Daemon,
    auth_token: str | None,
    jwt_config: JWTConfig | None,
    audit: Any,
    ws_max: int,
    websocket_auth_or_close: Any,
) -> None:
    """Attach the ``/api/v1/events`` WebSocket endpoint to ``app``."""
    _ws_connection_count: int = 0
    _ws_connection_lock = asyncio.Lock()

    async def _ws_acquire() -> bool:
        nonlocal _ws_connection_count
        if ws_max == 0:
            async with _ws_connection_lock:
                _ws_connection_count += 1
            return True
        async with _ws_connection_lock:
            if _ws_connection_count >= ws_max:
                return False
            _ws_connection_count += 1
        return True

    async def _ws_release() -> None:
        nonlocal _ws_connection_count
        async with _ws_connection_lock:
            _ws_connection_count = max(0, _ws_connection_count - 1)

    @app.websocket("/api/v1/events")
    async def events_ws(ws: WebSocket) -> None:
        """Stream daemon events as JSON frames to the client.

        Clients pass ``?token=...`` as a query param because browser WebSocket
        APIs cannot set headers.
        """
        if not await _ws_acquire():
            await ws.close(code=1013)  # 1013 = Try Again Later
            return
        try:
            if not await websocket_auth_or_close(
                ws,
                Role.viewer,
                auth_token,
                jwt_config,
                getattr(daemon, "_auth_store", None),
                audit,
            ):
                return
            await ws.accept()
            try:
                async for event in daemon.events.subscribe(queue_size=64):
                    await ws.send_text(event.to_json())
            except WebSocketDisconnect:
                return
            except Exception:  # noqa: BLE001
                await ws.close(code=1011)
        finally:
            await _ws_release()
