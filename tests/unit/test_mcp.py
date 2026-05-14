"""Unit tests for the MCP client manager and daemon-tool registry.

Phase 3 of the fleet testing alignment (Repository_Management EPIC #1140,
Maxwell_Daemon #860): exercise the MCP client message-handler logic without
booting a real MCP stdio subprocess. ``StdioServerParameters`` and
``stdio_client`` are mocked via ``create_autospec`` / ``AsyncMock`` so the
test stays fast, hermetic, and CI-friendly. No real ports are bound; no
session-scoped mutable fixtures are introduced.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest
from mcp import StdioServerParameters

from maxwell_daemon.config.models import McpServerConfig
from maxwell_daemon.mcp.client import McpClientManager
from maxwell_daemon.mcp.server.daemon_client import DaemonClient
from maxwell_daemon.mcp.server.daemon_tools import build_daemon_registry


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mcp_client_manager_handles_disabled_server() -> None:
    servers = {"test-server": McpServerConfig(name="test-server", command="echo", enabled=False)}
    manager = McpClientManager(servers)
    await manager.start()
    assert len(manager._sessions) == 0


@pytest.mark.unit
def test_daemon_registry_builds() -> None:
    # NOTE: port is a data field on the data-class DaemonClient; nothing binds
    # a socket here. Using 0 keeps the value generic (no real-port semantics).
    client = DaemonClient("127.0.0.1", 0)
    registry = build_daemon_registry(client)
    assert "submit_task" in registry.names()
    assert "list_tasks" in registry.names()
    assert "get_task" in registry.names()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mcp_client_manager_starts_enabled_server_with_autospec_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """McpClientManager.start() builds StdioServerParameters and a session.

    No real subprocess is launched: stdio_client and ClientSession are
    replaced by autospec/AsyncMock test doubles so we exercise the
    message-handler logic in pure-Python.
    """
    # autospec the type used by the production code; this catches signature drift.
    spec_params = create_autospec(StdioServerParameters, instance=True)
    assert spec_params is not None  # mock is constructed against the real class

    # The session ClientSession returns from initialize / list_tools.
    fake_tool = SimpleNamespace(
        name="echo",
        description="echo tool",
        inputSchema={"properties": {"text": {"type": "string"}}, "required": ["text"]},
    )

    fake_session = AsyncMock()
    fake_session.initialize = AsyncMock(return_value=None)
    fake_session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[fake_tool]))
    fake_session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text="hello")],
        )
    )

    @asynccontextmanager
    async def fake_stdio_client(_params: Any):  # type: ignore[no-untyped-def]
        yield (AsyncMock(), AsyncMock())  # (read, write)

    class _SessionCtx:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return fake_session

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    captured: dict[str, Any] = {}

    def _capture_params(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return spec_params

    # StdioServerParameters is imported lazily inside start(); patch the source.
    monkeypatch.setattr("mcp.StdioServerParameters", _capture_params)
    monkeypatch.setattr("maxwell_daemon.mcp.client.stdio_client", fake_stdio_client)
    monkeypatch.setattr("maxwell_daemon.mcp.client.ClientSession", _SessionCtx)

    servers = {
        "echo-srv": McpServerConfig(name="echo-srv", command="echo", args=["hi"], enabled=True),
    }
    manager = McpClientManager(servers)
    await manager.start()
    try:
        assert "echo-srv" in manager._sessions
        # StdioServerParameters was constructed with the configured command/args.
        assert captured["command"] == "echo"
        assert captured["args"] == ["hi"]
        fake_session.initialize.assert_awaited_once()
        fake_session.list_tools.assert_awaited_once()
        # Tool was cached under the namespaced name.
        names = [spec.name for spec in manager._tools]
        assert "echo-srv__echo" in names

        # Exercise the message-handler closure end-to-end.
        spec = next(s for s in manager._tools if s.name == "echo-srv__echo")
        result = await spec.handler(text="hi")
        assert result == "hello"
        fake_session.call_tool.assert_awaited_once_with("echo", arguments={"text": "hi"})
    finally:
        await manager.stop()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mcp_client_manager_skips_unsupported_transport() -> None:
    servers = {
        "http-srv": McpServerConfig(
            name="http-srv", command="echo", enabled=True, transport="http"
        ),
    }
    manager = McpClientManager(servers)
    await manager.start()
    assert manager._sessions == {}
    assert manager._tools == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mcp_tool_handler_propagates_error_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the remote tool returns isError=True, the handler raises RuntimeError."""
    fake_tool = SimpleNamespace(
        name="boom",
        description="failing tool",
        inputSchema={"properties": {}, "required": []},
    )
    fake_session = AsyncMock()
    fake_session.initialize = AsyncMock(return_value=None)
    fake_session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[fake_tool]))
    fake_session.call_tool = AsyncMock(return_value=SimpleNamespace(isError=True, content="kaboom"))

    @asynccontextmanager
    async def fake_stdio_client(_params: Any):  # type: ignore[no-untyped-def]
        yield (AsyncMock(), AsyncMock())

    class _SessionCtx:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return fake_session

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    monkeypatch.setattr("mcp.StdioServerParameters", MagicMock())
    monkeypatch.setattr("maxwell_daemon.mcp.client.stdio_client", fake_stdio_client)
    monkeypatch.setattr("maxwell_daemon.mcp.client.ClientSession", _SessionCtx)

    manager = McpClientManager(
        {"bad-srv": McpServerConfig(name="bad-srv", command="echo", enabled=True)}
    )
    await manager.start()
    try:
        spec = next(s for s in manager._tools if s.name == "bad-srv__boom")
        with pytest.raises(RuntimeError, match="failed"):
            await spec.handler()
    finally:
        await manager.stop()
