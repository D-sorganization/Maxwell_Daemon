"""Agent-agnostic tool definitions.

Tools are declared once as ``ToolSpec`` (or via the ``@mcp_tool`` decorator) and
compiled to provider-specific schemas on demand. The same handler serves every
backend — Anthropic, OpenAI, Ollama, or any model we add later — which keeps the
tool layer DRY and makes swapping backends a config change, not a code change.
"""

from maxwell_daemon.tools.builtins import (
    BashRunner,
    SandboxViolationError,
    build_default_registry,
)
from maxwell_daemon.tools.mcp import (
    ToolCapability,
    ToolInvocationRecord,
    ToolInvocationStore,
    ToolParam,
    ToolPolicy,
    ToolRegistry,
    ToolRegistryError,
    ToolResult,
    ToolRiskLevel,
    ToolSpec,
    mcp_tool,
)

__all__ = [
    "BashRunner",
    "SandboxViolationError",
    "ToolCapability",
    "ToolInvocationRecord",
    "ToolInvocationStore",
    "ToolParam",
    "ToolPolicy",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolResult",
    "ToolRiskLevel",
    "ToolSpec",
    "build_default_registry",
    "mcp_tool",
]
