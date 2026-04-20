"""Agent-agnostic tool definitions.

Tools are declared once as ``ToolSpec`` (or via the ``@mcp_tool`` decorator) and
compiled to provider-specific schemas on demand. The same handler serves every
backend — Anthropic, OpenAI, Ollama, or any model we add later — which keeps the
tool layer DRY and makes swapping backends a config change, not a code change.
"""

from conductor.tools.mcp import (
    ToolParam,
    ToolRegistry,
    ToolRegistryError,
    ToolResult,
    ToolSpec,
    mcp_tool,
)

__all__ = [
    "ToolParam",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolResult",
    "ToolSpec",
    "mcp_tool",
]
