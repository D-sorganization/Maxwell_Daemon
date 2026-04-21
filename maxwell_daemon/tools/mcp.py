"""Model Context Protocol — agent-agnostic tool declarations.

Every tool is a ``ToolSpec`` (name + description + params + handler). A registry
collects specs and emits provider-specific schemas (Anthropic ``tools`` dicts,
OpenAI function-calling dicts, …) so one set of handlers serves every backend.

The decorator ``@mcp_tool`` attaches a ``ToolSpec`` to a function so it can be
registered in bulk without repeating metadata.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

__all__ = [
    "ApprovalTierError",
    "HookRunnerProtocol",
    "ToolHandler",
    "ToolParam",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolResult",
    "ToolSpec",
    "mcp_tool",
]


class _PreToolOutcome(Protocol):
    """What a pre_tool hook runner returns — structural, no import cycle."""

    blocked: bool
    detail: str
    failing_command: str


class _PostToolOutcome(Protocol):
    """What a post_tool hook runner returns — structural, no import cycle."""

    errored: bool
    detail: str
    failing_command: str


class HookRunnerProtocol(Protocol):
    """The structural contract ToolRegistry expects for hook integration.

    ``maxwell_daemon.hooks.HookRunner`` satisfies this protocol; tests can
    substitute any object with the same method shape.
    """

    async def run_pre_tool(self, tool_name: str, tool_input: dict[str, Any]) -> _PreToolOutcome: ...
    async def run_post_tool(
        self, tool_name: str, tool_input: dict[str, Any], *, tool_output: str
    ) -> _PostToolOutcome: ...


#: JSON-schema primitive types we model in tool params. Complex structures are
#: represented as ``"object"`` or ``"array"``; a tool that needs nested validation
#: should do it inside the handler.
ParamType = Literal["string", "integer", "number", "boolean", "array", "object"]

ToolHandler = Callable[..., Any] | Callable[..., Awaitable[Any]]

F = TypeVar("F", bound=Callable[..., Any])


class ToolRegistryError(Exception):
    """Raised when the registry is asked to do something inconsistent."""


class ApprovalTierError(Exception):
    """Raised when a tool invocation is blocked by the configured approval tier."""


@dataclass(slots=True, frozen=True)
class ToolParam:
    """A declared parameter on a tool."""

    name: str
    type: ParamType
    description: str
    required: bool = True
    enum: list[Any] | None = None

    def to_schema(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum is not None:
            out["enum"] = list(self.enum)
        return out


@dataclass(slots=True, frozen=True)
class ToolResult:
    """Outcome of a tool invocation.

    ``is_error=True`` signals a handler failure in a form the model can see and
    recover from; the exception is rendered into ``content`` so the agent can
    decide what to do next.
    """

    content: str
    is_error: bool = False


def _noop_handler() -> None:
    return None


@dataclass(slots=True)
class ToolSpec:
    """Declarative tool definition used by every backend."""

    name: str
    description: str
    params: list[ToolParam] = field(default_factory=list)
    handler: ToolHandler = field(default=_noop_handler)

    def _schema_body(self) -> dict[str, Any]:
        properties: dict[str, Any] = {p.name: p.to_schema() for p in self.params}
        required = [p.name for p in self.params if p.required]
        return {"type": "object", "properties": properties, "required": required}

    def to_anthropic(self) -> dict[str, Any]:
        """Emit the dict the Anthropic SDK expects in ``messages.create(tools=[…])``."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self._schema_body(),
        }

    def to_openai(self) -> dict[str, Any]:
        """Emit the dict the OpenAI SDK expects for function calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._schema_body(),
            },
        }


class ToolRegistry:
    """Holds ``ToolSpec``s. Emits schemas. Dispatches calls.

    When a ``hook_runner`` is supplied, every :meth:`invoke` fires
    ``pre_tool`` (which can block the call) and ``post_tool`` (which can
    turn a successful tool call into an agent-visible error). Default
    ``hook_runner=None`` preserves byte-for-byte pre-existing behaviour.

    Parameters
    ----------
    approval_tier:
        Controls whether tool handlers may execute automatically (#237).
        ``"full-auto"`` (default) runs handlers without restriction.
        ``"auto-edit"`` runs handlers but is intended for supervised edit
        workflows.
        ``"suggest"`` blocks *all* automatic execution — every invocation
        returns an error result requesting human approval instead.
    """

    #: Tiers that permit automatic handler execution (lowest to highest).
    _AUTO_EXECUTE_TIERS: frozenset[str] = frozenset({"auto-edit", "full-auto"})

    def __init__(
        self,
        *,
        hook_runner: HookRunnerProtocol | None = None,
        approval_tier: str = "full-auto",
    ) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._hook_runner = hook_runner
        self._approval_tier = approval_tier

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ToolRegistryError(f"tool {spec.name!r} already registered")
        self._specs[spec.name] = spec

    def register_from_function(self, fn: Callable[..., Any]) -> None:
        """Register a function previously decorated with ``@mcp_tool``."""
        spec = getattr(fn, "__mcp_tool__", None)
        if not isinstance(spec, ToolSpec):
            raise ToolRegistryError(f"{fn!r} is not decorated with @mcp_tool — nothing to register")
        self.register(spec)

    def get(self, name: str) -> ToolSpec:
        if name not in self._specs:
            raise ToolRegistryError(f"unknown tool {name!r}")
        return self._specs[name]

    def names(self) -> list[str]:
        return sorted(self._specs.keys())

    def to_anthropic(self) -> list[dict[str, Any]]:
        return [s.to_anthropic() for s in self._specs.values()]

    def to_openai(self) -> list[dict[str, Any]]:
        return [s.to_openai() for s in self._specs.values()]

    async def invoke(
        self, name: str, arguments: dict[str, Any], approval_tier: str | None = None
    ) -> ToolResult:
        """Call the handler for ``name`` with ``arguments``.

        Approval tier enforcement (#237):
          The registry's ``approval_tier`` (set at construction) is checked
          *before* any hook or handler runs. When the tier is ``"suggest"``,
          every invocation is blocked and a ``ToolResult(is_error=True)`` is
          returned so the agent can surface the request for human review.

          The optional *approval_tier* parameter accepted here is kept for
          call-site compatibility but the registry-level tier takes precedence;
          pass a new ``ToolRegistry`` instance with the desired tier instead.

        Hook phases (only when ``hook_runner`` was passed at construction):

          1. ``pre_tool`` — if any pre-tool hook blocks, we return an error
             ``ToolResult`` without calling the handler.
          2. handler — errors surface as ``ToolResult(is_error=True)``.
          3. ``post_tool`` — only fires when the handler succeeded. Hook
             errors turn the success into an agent-visible error while
             preserving the original output for the agent's reference.
        """
        spec = self.get(name)  # raises ToolRegistryError on unknown — caller bug, not model bug

        # Enforce approval tier before running any hooks or the handler (#237).
        if self._approval_tier not in self._AUTO_EXECUTE_TIERS:
            return ToolResult(
                content=(
                    f"Tool '{name}' requires human approval "
                    f"(approval_tier={self._approval_tier!r}). "
                    "The request has been surfaced for review and will not execute automatically."
                ),
                is_error=True,
            )

        if self._hook_runner is not None:
            try:
                pre = await self._hook_runner.run_pre_tool(name, arguments)
            except Exception as exc:
                return ToolResult(
                    content=f"pre_tool hook runner error: {type(exc).__name__}: {exc}",
                    is_error=True,
                )
            if pre.blocked:
                return ToolResult(
                    content=(
                        f"pre_tool hook refused the call: {pre.failing_command}\n{pre.detail}"
                    ),
                    is_error=True,
                )

        try:
            result = spec.handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            content = _stringify(result)
        except Exception as exc:
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)

        if self._hook_runner is not None:
            try:
                post = await self._hook_runner.run_post_tool(name, arguments, tool_output=content)
            except Exception as exc:
                return ToolResult(
                    content=(
                        f"{content}\n\npost_tool hook runner error: {type(exc).__name__}: {exc}"
                    ),
                    is_error=True,
                )
            if post.errored:
                return ToolResult(
                    content=(
                        f"{content}\n\n"
                        f"post_tool hook error ({post.failing_command}):\n{post.detail}"
                    ),
                    is_error=True,
                )

        return ToolResult(content=content, is_error=False)


def mcp_tool(
    *,
    description: str,
    params: list[ToolParam] | None = None,
    name: str | None = None,
) -> Callable[[F], F]:
    """Attach a ``ToolSpec`` to a function so a registry can pick it up.

    The wrapped function is returned unchanged — we only stamp ``__mcp_tool__``
    on it so the registry can inspect it later. Default ``name`` is the
    function's own ``__name__``.
    """

    def decorator(fn: F) -> F:
        fn_name = getattr(fn, "__name__", "tool") or "tool"
        spec = ToolSpec(
            name=name or fn_name,
            description=description,
            params=list(params or []),
            handler=fn,
        )
        fn.__mcp_tool__ = spec  # type: ignore[attr-defined]
        return fn

    return decorator


def _stringify(value: Any) -> str:
    """Coerce a handler return to the string representation backends expect.

    ``None`` becomes an empty string so empty returns don't surface as the
    literal ``"None"`` to the model — which would be confusing context.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return repr(value)
