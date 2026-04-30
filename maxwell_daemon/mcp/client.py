"""Model Context Protocol (MCP) Client."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from maxwell_daemon.config.models import McpServerConfig
from maxwell_daemon.logging import get_logger
from maxwell_daemon.tools.mcp import ToolParam, ToolRegistry, ToolSpec

log = get_logger(__name__)


class McpClientManager:
    """Manages connections to remote MCP servers and bridges their tools to the ToolRegistry."""

    def __init__(self, servers: dict[str, McpServerConfig]) -> None:
        self._configs = servers
        self._exit_stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tools: list[ToolSpec] = []

    async def start(self) -> None:
        """Start all enabled MCP servers and cache their tools."""
        for name, config in self._configs.items():
            if not config.enabled:
                continue

            try:
                if config.transport != "stdio":
                    log.warning(
                        "Unsupported MCP transport %r for server %r",
                        config.transport,
                        name,
                    )
                    continue

                # Pass through the process env as well as any configured overrides
                import os

                env = dict(os.environ)
                if config.env:
                    env.update(config.env)

                server_params = StdioServerParameters(
                    command=config.command, args=config.args, env=env
                )

                stdio_transport = await self._exit_stack.enter_async_context(
                    stdio_client(server_params)
                )
                read, write = stdio_transport
                session = await self._exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session
                log.info("MCP server %r connected", name)

                # Cache tools at startup
                result = await session.list_tools()
                for mcp_tool in getattr(result, "tools", []):
                    self._tools.append(self._create_tool_spec(name, mcp_tool, session))

            except Exception as e:
                log.exception("Failed to start MCP server %r: %s", name, e)

    def _create_tool_spec(
        self, server_name: str, mcp_tool: Any, session: ClientSession
    ) -> ToolSpec:
        params = []
        if isinstance(mcp_tool.inputSchema, dict):
            props = mcp_tool.inputSchema.get("properties", {})
            required = mcp_tool.inputSchema.get("required", [])
        else:
            props = getattr(mcp_tool.inputSchema, "properties", {})
            required = getattr(mcp_tool.inputSchema, "required", [])

        for p_name, p_schema in props.items():
            params.append(
                ToolParam(
                    name=p_name,
                    type=p_schema.get("type", "string"),
                    description=p_schema.get("description", ""),
                    required=p_name in required,
                    enum=p_schema.get("enum"),
                )
            )

        # Create closure for handler
        async def handler(
            *args: Any,
            _session: ClientSession = session,
            _tool_name: str = mcp_tool.name,
            **kwargs: Any,
        ) -> str:
            try:
                res = await _session.call_tool(_tool_name, arguments=kwargs)
                if getattr(res, "isError", False):
                    raise RuntimeError(str(getattr(res, "content", "Unknown error")))

                # Content is often a list of TextContent objects
                content_parts = []
                for item in getattr(res, "content", []):
                    if hasattr(item, "text"):
                        content_parts.append(item.text)
                    elif isinstance(item, dict) and "text" in item:
                        content_parts.append(item["text"])
                    else:
                        content_parts.append(str(item))

                return "\n".join(content_parts)
            except Exception as e:
                raise RuntimeError(f"MCP tool {server_name}.{_tool_name} failed: {e}") from e

        tool_name = f"{server_name}__{mcp_tool.name}"
        return ToolSpec(
            name=tool_name,
            description=mcp_tool.description or f"MCP tool {mcp_tool.name} from {server_name}",
            params=params,
            handler=handler,
            risk_level="external_side_effect",
            requires_approval=True,
        )

    async def stop(self) -> None:
        """Stop all MCP servers."""
        await self._exit_stack.aclose()

    def attach_tools(self, registry: ToolRegistry) -> None:
        """Register all cached MCP tools onto the given registry."""
        for spec in self._tools:
            try:
                registry.register(spec)
                log.debug("Attached MCP tool %r", spec.name)
            except Exception as e:  # noqa: BLE001
                log.warning("Failed to attach MCP tool %r: %s", spec.name, e)
