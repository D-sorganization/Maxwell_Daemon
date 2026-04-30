"""Model Context Protocol (MCP) Server."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
)
from pydantic import AnyUrl

from maxwell_daemon.config import load_config
from maxwell_daemon.core.action_service import ActionService
from maxwell_daemon.core.action_store import ActionStore
from maxwell_daemon.core.cross_audit import DEFAULT_CROSS_AUDIT_ROLES
from maxwell_daemon.mcp.server.daemon_client import DaemonClient
from maxwell_daemon.mcp.server.daemon_tools import build_daemon_registry
from maxwell_daemon.tools.builtins import build_default_registry

log = logging.getLogger(__name__)


async def run_mcp_server(config_path: Path | None = None) -> None:  # noqa: C901
    """Run the Maxwell Daemon as an MCP server via stdio."""
    config = load_config(config_path)

    server = Server("maxwell-daemon")

    # Wire up the ActionService so side-effecting tools require approval in the daemon UI
    action_store = ActionStore(":memory:")
    action_service = ActionService(action_store)

    # We expose the built-in sandbox tools mapped to the default workspace.
    registry = build_default_registry(config.memory.workspace_path, action_service=action_service)

    # Expose the daemon tools via REST API proxy
    client = DaemonClient(config.api.host, config.api.port, config.api.auth_token)
    daemon_registry = build_daemon_registry(client)

    for name in daemon_registry.names():
        registry.register(daemon_registry.get(name))

    @server.list_tools()  # type: ignore
    async def handle_list_tools() -> list[Tool]:
        mcp_tools = []
        for name in registry.names():
            spec = registry.get(name)

            # Map ToolParam to JSON Schema
            schema: dict[str, Any] = {
                "type": "object",
                "properties": {},
                "required": [],
            }
            for param in spec.params:
                schema["properties"][param.name] = {
                    "type": param.type,
                    "description": param.description,
                }
                if param.enum:
                    schema["properties"][param.name]["enum"] = param.enum
                if param.required:
                    schema["required"].append(param.name)

            mcp_tools.append(Tool(name=spec.name, description=spec.description, inputSchema=schema))
        return mcp_tools

    @server.call_tool()  # type: ignore
    async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        try:
            # We enforce that all MCP calls pass through the audit/approval tier by default
            # if the tool was created with requires_approval, but here the UI handles approval.
            result = await registry.invoke(name, arguments or {})
            if result.is_error:
                return [TextContent(type="text", text=f"Error: {result.content}")]
            return [TextContent(type="text", text=result.content)]
        except Exception as e:
            log.exception("Tool execution failed: %s", name)
            return [TextContent(type="text", text=f"Tool exception: {e}")]

    @server.list_resources()  # type: ignore
    async def handle_list_resources() -> list[Resource]:
        return [
            Resource(
                uri=AnyUrl("artifact://list"),
                name="Artifacts",
                description="Maxwell Daemon artifacts",
            ),
            Resource(
                uri=AnyUrl("workspace://list"),
                name="Workspaces",
                description="Task workspaces",
            ),
            Resource(
                uri=AnyUrl("memory://list"),
                name="Episodic Memory",
                description="Agent memory",
            ),
        ]

    @server.read_resource()  # type: ignore
    async def handle_read_resource(uri: AnyUrl | str) -> str:
        return f"Resource {uri} is not fully implemented yet over REST proxy."

    @server.list_prompts()  # type: ignore
    async def handle_list_prompts() -> list[Prompt]:
        prompts = []
        for role_id, role in DEFAULT_CROSS_AUDIT_ROLES.items():
            prompts.append(
                Prompt(
                    name=f"maxwell_{role_id}",
                    description=f"Maxwell: {role.name}",
                    arguments=[],
                )
            )
        return prompts

    @server.get_prompt()  # type: ignore
    async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
        role_id = name.replace("maxwell_", "")
        role = DEFAULT_CROSS_AUDIT_ROLES.get(role_id)
        if not role:
            raise ValueError(f"Unknown prompt: {name}")

        return GetPromptResult(
            description=role.name,
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(type="text", text=role.system_prompt),
                )
            ],
        )

    options = server.create_initialization_options()
    async with stdio_server() as (read, write):
        await server.run(read, write, options)
