"""Model Context Protocol — agent-agnostic tool declarations.

Every tool is a ``ToolSpec`` (name + description + params + handler). A registry
collects specs and emits provider-specific schemas (Anthropic ``tools`` dicts,
OpenAI function-calling dicts, …) so one set of handlers serves every backend.

The decorator ``@mcp_tool`` attaches a ``ToolSpec`` to a function so it can be
registered in bulk without repeating metadata.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar
from uuid import uuid4

__all__ = [
    "ApprovalTierError",
    "HookRunnerProtocol",
    "ToolCapability",
    "ToolHandler",
    "ToolInvocationRecord",
    "ToolInvocationStore",
    "ToolParam",
    "ToolPolicy",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolResult",
    "ToolRiskLevel",
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
ToolCapability = Literal[
    "repo_read",
    "repo_write",
    "shell_read",
    "shell_write",
    "network",
    "github_read",
    "github_write",
    "file_read",
    "file_write",
    "artifact_write",
]
ToolRiskLevel = Literal[
    "read_only",
    "local_write",
    "command_execution",
    "network_write",
    "external_side_effect",
    "destructive",
]

ToolHandler = Callable[..., Any] | Callable[..., Awaitable[Any]]

F = TypeVar("F", bound=Callable[..., Any])

_RISK_ORDER: dict[ToolRiskLevel, int] = {
    "read_only": 0,
    "local_write": 1,
    "command_execution": 2,
    "network_write": 3,
    "external_side_effect": 4,
    "destructive": 5,
}
_SENSITIVE_ARGUMENT_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "cookie",
        "password",
        "secret",
        "ssh_key",
        "token",
        "x-api-key",
    }
)


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


@dataclass(slots=True, frozen=True)
class ToolPolicy:
    """Policy gate for registry-managed tool execution."""

    allowed_tool_ids: frozenset[str] | None = None
    denied_tool_ids: frozenset[str] = frozenset()
    allowed_capabilities: frozenset[ToolCapability] | None = None
    denied_capabilities: frozenset[ToolCapability] = frozenset()
    max_risk_level_without_approval: ToolRiskLevel = "read_only"

    @classmethod
    def readonly_default(cls) -> ToolPolicy:
        return cls(
            allowed_capabilities=frozenset({"repo_read", "file_read", "github_read"}),
            max_risk_level_without_approval="read_only",
        )

    def denial_reason(self, spec: ToolSpec) -> str | None:
        if spec.name in self.denied_tool_ids:
            return f"tool {spec.name!r} is denied by policy"
        if self.allowed_tool_ids is not None and spec.name not in self.allowed_tool_ids:
            return f"tool {spec.name!r} is not in the allowed tool set"

        capabilities = frozenset(spec.capabilities)
        denied = sorted(capabilities & self.denied_capabilities)
        if denied:
            return f"tool {spec.name!r} has denied capabilities: {', '.join(denied)}"
        if self.allowed_capabilities is not None:
            if not capabilities:
                return f"tool {spec.name!r} is unclassified and denied under capability allowlist"
            unallowed = sorted(capabilities - self.allowed_capabilities)
            if unallowed:
                return f"tool {spec.name!r} has unallowed capabilities: {', '.join(unallowed)}"

        if _RISK_ORDER[spec.risk_level] > _RISK_ORDER[self.max_risk_level_without_approval]:
            return (
                f"tool {spec.name!r} risk level {spec.risk_level!r} exceeds "
                f"policy maximum {self.max_risk_level_without_approval!r}"
            )
        if spec.requires_approval:
            return f"tool {spec.name!r} requires approval"
        return None


@dataclass(slots=True, frozen=True)
class ToolInvocationRecord:
    """Append-oriented audit record for a registry invocation attempt."""

    id: str
    timestamp: str
    tool_name: str
    status: str
    redacted_arguments: dict[str, Any]
    result_summary: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "tool_name": self.tool_name,
            "status": self.status,
            "redacted_arguments": self.redacted_arguments,
            "result_summary": self.result_summary,
            "error": self.error,
        }


class ToolInvocationStore:
    """Durable JSONL store for tool invocation attempts."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._records: list[ToolInvocationRecord] = []

    @property
    def records(self) -> list[ToolInvocationRecord]:
        return list(self._records)

    def append(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        status: str,
        result_summary: str | None = None,
        error: str | None = None,
    ) -> ToolInvocationRecord:
        record = ToolInvocationRecord(
            id=str(uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            tool_name=tool_name,
            status=status,
            redacted_arguments=_redact_arguments(arguments),
            result_summary=result_summary,
            error=error,
        )
        self._records.append(record)
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.as_dict(), separators=(",", ":")) + "\n")
        return record


def _noop_handler() -> None:
    return None


@dataclass(slots=True)
class ToolSpec:
    """Declarative tool definition used by every backend."""

    name: str
    description: str
    params: list[ToolParam] = field(default_factory=list)
    handler: ToolHandler = field(default=_noop_handler)
    capabilities: frozenset[ToolCapability] = frozenset()
    risk_level: ToolRiskLevel = "read_only"
    requires_approval: bool = False

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
        policy: ToolPolicy | None = None,
        invocation_store: ToolInvocationStore | None = None,
    ) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._hook_runner = hook_runner
        self._approval_tier = approval_tier
        self._policy = policy
        self._invocation_store = invocation_store

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
        self,
        name: str,
        arguments: dict[str, Any],
        approval_tier: str | None = None,
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

        if self._policy is not None:
            denial_reason = self._policy.denial_reason(spec)
            if denial_reason is not None:
                self._record_invocation(
                    name,
                    arguments,
                    status="denied",
                    error=denial_reason,
                )
                return ToolResult(
                    content=f"Tool invocation denied by policy: {denial_reason}",
                    is_error=True,
                )

        # Enforce approval tier before running any hooks or the handler (#237).
        if self._approval_tier not in self._AUTO_EXECUTE_TIERS:
            self._record_invocation(
                name,
                arguments,
                status="approval_required",
                error=f"approval_tier={self._approval_tier!r}",
            )
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
            except Exception as exc:  # noqa: BLE001
                self._record_invocation(
                    name,
                    arguments,
                    status="failed",
                    error=f"pre_tool hook runner error: {type(exc).__name__}: {exc}",
                )
                return ToolResult(
                    content=f"pre_tool hook runner error: {type(exc).__name__}: {exc}",
                    is_error=True,
                )
            if pre.blocked:
                self._record_invocation(
                    name,
                    arguments,
                    status="denied",
                    error=f"pre_tool hook refused the call: {pre.failing_command}",
                )
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
        except Exception as exc:  # noqa: BLE001
            self._record_invocation(
                name,
                arguments,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)

        if self._hook_runner is not None:
            post = await self._hook_runner.run_post_tool(name, arguments, tool_output=content)
            if post.errored:
                self._record_invocation(
                    name,
                    arguments,
                    status="failed",
                    result_summary=content[:200],
                    error=f"post_tool hook error: {post.failing_command}",
                )
                return ToolResult(
                    content=(
                        f"{content}\n\n"
                        f"post_tool hook error ({post.failing_command}):\n{post.detail}"
                    ),
                    is_error=True,
                )

        self._record_invocation(name, arguments, status="succeeded", result_summary=content[:200])
        return ToolResult(content=content, is_error=False)

    def _record_invocation(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        status: str,
        result_summary: str | None = None,
        error: str | None = None,
    ) -> None:
        if self._invocation_store is None:
            return
        try:
            self._invocation_store.append(
                tool_name=tool_name,
                arguments=arguments,
                status=status,
                result_summary=result_summary,
                error=error,
            )
        except Exception as exc:  # noqa: BLE001
            import structlog

            log = structlog.get_logger(__name__)
            log.warning(
                "failed to record tool invocation",
                tool_name=tool_name,
                status=status,
                error=f"{type(exc).__name__}: {exc}",
            )


def mcp_tool(
    *,
    description: str,
    params: list[ToolParam] | None = None,
    name: str | None = None,
    capabilities: frozenset[ToolCapability] | None = None,
    risk_level: ToolRiskLevel = "read_only",
    requires_approval: bool = False,
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
            capabilities=capabilities or frozenset(),
            risk_level=risk_level,
            requires_approval=requires_approval,
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


def _redact_arguments(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in _SENSITIVE_ARGUMENT_KEYS:
                redacted[key] = "***"
            elif isinstance(item, str) and item.lower().startswith("bearer "):
                redacted[key] = "Bearer ***"
            else:
                redacted[key] = _redact_arguments(item)
        return redacted
    if isinstance(value, list):
        return [_redact_arguments(item) for item in value]
    return value
