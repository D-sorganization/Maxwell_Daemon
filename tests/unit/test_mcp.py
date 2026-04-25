import pytest

from maxwell_daemon.config.models import McpServerConfig
from maxwell_daemon.mcp.client import McpClientManager
from maxwell_daemon.mcp.server.daemon_client import DaemonClient
from maxwell_daemon.mcp.server.daemon_tools import build_daemon_registry


@pytest.mark.asyncio
async def test_mcp_client_manager_handles_disabled_server():  # type: ignore[no-untyped-def]
    servers = {"test-server": McpServerConfig(name="test-server", command="echo", enabled=False)}
    manager = McpClientManager(servers)
    await manager.start()
    assert len(manager._sessions) == 0


def test_daemon_registry_builds():  # type: ignore[no-untyped-def]
    client = DaemonClient("127.0.0.1", 8080)
    registry = build_daemon_registry(client)
    assert "submit_task" in registry.names()
    assert "list_tasks" in registry.names()
    assert "get_task" in registry.names()
