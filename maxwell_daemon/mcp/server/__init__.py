"""Model Context Protocol (MCP) Server."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    GetPromptRequestParams,
    GetPromptResult,
    ListPromptsResult,
    ListResourcesResult,
    ListToolsResult,
    PaginatedRequestParams,
    Prompt,
    PromptMessage,
    ReadResourceRequestParams,
    ReadResourceResult,
    Resource,
    TextContent,
    TextResourceContents,
    Tool,
)

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

    async def handle_list_tools(
        _ctx: ServerRequestContext[Any], _params: PaginatedRequestParams | None
    ) -> ListToolsResult:
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

            mcp_tools.append(
                Tool(name=spec.name, description=spec.description, input_schema=schema)
            )
        return ListToolsResult(tools=mcp_tools)

    async def handle_call_tool(
        _ctx: ServerRequestContext[Any], params: CallToolRequestParams
    ) -> CallToolResult:
        try:
            # We enforce that all MCP calls pass through the audit/approval tier by default
            # if the tool was created with requires_approval, but here the UI handles approval.
            result = await registry.invoke(params.name, params.arguments or {})
            if result.is_error:
                return CallToolResult(
                    content=[TextContent(type="text", text=f"Error: {result.content}")],
                    is_error=True,
                )
            return CallToolResult(content=[TextContent(type="text", text=result.content)])
        except Exception as e:
            log.exception("Tool execution failed: %s", params.name)
            return CallToolResult(
                content=[TextContent(type="text", text=f"Tool exception: {e}")],
                is_error=True,
            )

    async def handle_list_resources(
        _ctx: ServerRequestContext[Any], _params: PaginatedRequestParams | None
    ) -> ListResourcesResult:
        return ListResourcesResult(
            resources=[
                Resource(
                    uri="artifact://list",
                    name="Artifacts",
                    description="Maxwell Daemon artifacts",
                ),
                Resource(
                    uri="workspace://list",
                    name="Workspaces",
                    description="Task workspaces",
                ),
                Resource(
                    uri="memory://list",
                    name="Episodic Memory",
                    description="Agent memory",
                ),
            ]
        )

    async def handle_read_resource(
        _ctx: ServerRequestContext[Any], params: ReadResourceRequestParams
    ) -> ReadResourceResult:
        uri = str(params.uri)
        return ReadResourceResult(
            contents=[
                TextResourceContents(
                    uri=uri,
                    mime_type="text/plain",
                    text=f"Resource {uri} is not fully implemented yet over REST proxy.",
                )
            ]
        )

    async def handle_list_prompts(
        _ctx: ServerRequestContext[Any], _params: PaginatedRequestParams | None
    ) -> ListPromptsResult:
        prompts = []
        for role_id, role in DEFAULT_CROSS_AUDIT_ROLES.items():
            prompts.append(
                Prompt(
                    name=f"maxwell_{role_id}",
                    description=f"Maxwell: {role.name}",
                    arguments=[],
                )
            )
        return ListPromptsResult(prompts=prompts)

    async def handle_get_prompt(
        _ctx: ServerRequestContext[Any], params: GetPromptRequestParams
    ) -> GetPromptResult:
        role_id = params.name.replace("maxwell_", "")
        role = DEFAULT_CROSS_AUDIT_ROLES.get(role_id)
        if not role:
            raise ValueError(f"Unknown prompt: {params.name}")

        return GetPromptResult(
            description=role.name,
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(type="text", text=role.system_prompt),
                )
            ],
        )

    server = Server(
        "maxwell-daemon",
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
        on_list_resources=handle_list_resources,
        on_read_resource=handle_read_resource,
        on_list_prompts=handle_list_prompts,
        on_get_prompt=handle_get_prompt,
    )
    options = server.create_initialization_options()
    async with stdio_server() as (read, write):
        await server.run(read, write, options)
