"""Model Context Protocol (MCP) Server."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from maxwell_daemon.config import load_config
from maxwell_daemon.tools.builtins import build_default_registry

log = logging.getLogger(__name__)


async def run_mcp_server(config_path: Path | None = None) -> None:
    """Run the Maxwell Daemon as an MCP server via stdio."""
    config = load_config(config_path)

    server = Server("maxwell-daemon")

    # We expose the built-in sandbox tools mapped to the default workspace.
    registry = build_default_registry(config.memory.workspace_path)

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
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

    @server.call_tool()  # type: ignore[untyped-decorator]
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

    options = server.create_initialization_options()
    async with stdio_server() as (read, write):
        await server.run(read, write, options)
