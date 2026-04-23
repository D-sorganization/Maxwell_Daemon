"""Contract layer for external agent adapters.

This module intentionally stays below policy, routing, and scheduling. It only
describes the shape of an external agent adapter, validates a few local
contract rules, and provides a structured unavailable fallback.
"""

from __future__ import annotations

import asyncio
import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Coroutine
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

from maxwell_daemon.backends.base import (
    BackendResponse,
    BackendUnavailableError,
    ILLMBackend,
    Message,
    MessageRole,
)
from maxwell_daemon.backends.claude_code import ClaudeCodeCLIBackend
from maxwell_daemon.backends.codex_cli import CodexCLIBackend
from maxwell_daemon.backends.continue_cli import ContinueCLIBackend
from maxwell_daemon.backends.jules_cli import JulesCLIBackend

__all__ = [
    "BackendReadOnlyExternalAgentAdapter",
    "ClaudeCodeCLIExternalAgentAdapter",
    "CodexCLIExternalAgentAdapter",
    "ContinueCLIExternalAgentAdapter",
    "ExternalAgentAdapterBase",
    "ExternalAgentAdapterError",
    "ExternalAgentAdapterProtocol",
    "ExternalAgentAdapterRegistry",
    "ExternalAgentCapability",
    "ExternalAgentOperation",
    "ExternalAgentProbeResult",
    "ExternalAgentProbeSpec",
    "ExternalAgentRunContext",
    "ExternalAgentRunResult",
    "ExternalAgentRunStatus",
    "JulesCLIExternalAgentAdapter",
    "UnavailableExternalAgentAdapter",
    "redact_secrets",
]


class ExternalAgentAdapterError(RuntimeError):
    """Raised when adapter registration or local contract validation fails."""


class ExternalAgentOperation(str, Enum):
    """Supported external-agent operations."""

    PROBE = "probe"
    PLAN = "plan"
    IMPLEMENT = "implement"
    REVIEW = "review"
    VALIDATE = "validate"
    CHECKPOINT = "checkpoint"
    CANCEL = "cancel"
    # Legacy compatibility with the first contract slice.
    READ = "read"
    WRITE = "write"


class ExternalAgentRunStatus(str, Enum):
    """Structured outcomes for adapter execution."""

    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"
    CANCELLED = "cancelled"


_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)(Authorization:\s*Basic)\s+\S+"),
        r"\1 ***",
    ),
    (
        re.compile(r"(?i)(Authorization:\s*Bearer)\s+\S+"),
        r"\1 ***",
    ),
    (
        re.compile(r"(?i)\bBearer\s+\S+"),
        "Bearer ***",
    ),
    (
        re.compile(r"(?i)\b(api[_-]?key|token|secret|password)=([^\s&]+)"),
        r"\1=***",
    ),
)

_T = TypeVar("_T")


def _operation_is_read_only(operation: ExternalAgentOperation) -> bool:
    return operation in {
        ExternalAgentOperation.PROBE,
        ExternalAgentOperation.PLAN,
        ExternalAgentOperation.REVIEW,
        ExternalAgentOperation.VALIDATE,
        ExternalAgentOperation.CHECKPOINT,
        ExternalAgentOperation.READ,
    }


def _default_read_only_operations() -> frozenset[ExternalAgentOperation]:
    return frozenset(
        operation for operation in ExternalAgentOperation if _operation_is_read_only(operation)
    )


def _default_write_operations() -> frozenset[ExternalAgentOperation]:
    return frozenset({ExternalAgentOperation.IMPLEMENT, ExternalAgentOperation.WRITE})


def _run_coroutine_sync(coroutine: Coroutine[Any, Any, _T]) -> _T:
    """Run an async backend call from the synchronous adapter contract."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: list[_T] = []
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:  # pragma: no cover - defensive thread handoff
            errors.append(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0]


def _snippet(text: str, *, limit: int = 4_000) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


def redact_secrets(text: str) -> str:
    """Redact common credential forms from diagnostic text."""

    redacted = text
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


@dataclass(slots=True, frozen=True)
class ExternalAgentCapability:
    """Declares what an adapter can do."""

    adapter_id: str | None = None
    display_name: str = "External agent"
    version: str | None = None
    probe_info: tuple[str, ...] = ()
    supported_roles: frozenset[str] = field(default_factory=frozenset)
    supported_operations: frozenset[ExternalAgentOperation] = field(
        default_factory=lambda: frozenset(ExternalAgentOperation)
    )
    read_only_operations: frozenset[ExternalAgentOperation] = field(
        default_factory=_default_read_only_operations
    )
    write_operations: frozenset[ExternalAgentOperation] = field(
        default_factory=_default_write_operations
    )
    capability_tags: frozenset[str] = field(default_factory=frozenset)
    context_limits: dict[str, int] = field(default_factory=dict)
    cost_model: str | None = None
    quota_model: str | None = None
    required_credentials: tuple[str, ...] = ()
    required_binaries: tuple[str, ...] = ()
    workspace_requirements: tuple[str, ...] = ()
    can_edit_files: bool = False
    can_run_tests: bool = False
    supports_background: bool = False
    supports_cancellation: bool = True
    safety_notes: tuple[str, ...] = ()

    def supports(self, operation: ExternalAgentOperation) -> bool:
        return operation in self.supported_operations

    def is_read_only(self, operation: ExternalAgentOperation) -> bool:
        return operation in self.read_only_operations

    def requires_workspace(self, operation: ExternalAgentOperation) -> bool:
        return operation in self.write_operations


@dataclass(slots=True, frozen=True)
class ExternalAgentRunContext:
    """Invocation context for an external adapter."""

    adapter_id: str
    operation: ExternalAgentOperation
    prompt: str
    workspace: Path | None = None
    task_id: str | None = None
    work_item_id: str | None = None
    gate_context: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    cancellation_requested: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        return _operation_is_read_only(self.operation)

    @property
    def has_workspace(self) -> bool:
        return self.workspace is not None


@dataclass(slots=True, frozen=True)
class ExternalAgentProbeResult:
    """Sanitized adapter diagnostics."""

    adapter_id: str
    summary: str
    version: str | None = None
    details: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    available: bool = True

    def redacted(self) -> ExternalAgentProbeResult:
        return replace(
            self,
            summary=redact_secrets(self.summary),
            details=tuple(redact_secrets(detail) for detail in self.details),
            metadata=_redact_value(self.metadata),
        )


@dataclass(slots=True, frozen=True)
class ExternalAgentRunResult:
    """Structured adapter outcome."""

    adapter_id: str
    operation: ExternalAgentOperation
    status: ExternalAgentRunStatus
    summary: str
    details: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    commands_run: tuple[str, ...] = ()
    tests_run: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    cost_estimate_usd: float | None = None
    quota_estimate: str | None = None
    stdout_snippet: str | None = None
    stderr_snippet: str | None = None
    checkpoint: str | None = None
    policy_warnings: tuple[str, ...] = ()
    read_only: bool = False
    cancellation_requested: bool = False
    cancellation_recorded: bool = False
    unavailable_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def completed(
        cls,
        *,
        adapter_id: str,
        operation: ExternalAgentOperation,
        summary: str,
        details: tuple[str, ...] | list[str] = (),
        changed_files: tuple[str, ...] | list[str] = (),
        commands_run: tuple[str, ...] | list[str] = (),
        tests_run: tuple[str, ...] | list[str] = (),
        artifacts: tuple[str, ...] | list[str] = (),
        cost_estimate_usd: float | None = None,
        quota_estimate: str | None = None,
        stdout_snippet: str | None = None,
        stderr_snippet: str | None = None,
        checkpoint: str | None = None,
        policy_warnings: tuple[str, ...] | list[str] = (),
        read_only: bool | None = None,
        cancellation_requested: bool = False,
        cancellation_recorded: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ExternalAgentRunResult:
        return cls(
            adapter_id=adapter_id,
            operation=operation,
            status=ExternalAgentRunStatus.COMPLETED,
            summary=summary,
            details=tuple(details),
            changed_files=tuple(changed_files),
            commands_run=tuple(commands_run),
            tests_run=tuple(tests_run),
            artifacts=tuple(artifacts),
            cost_estimate_usd=cost_estimate_usd,
            quota_estimate=quota_estimate,
            stdout_snippet=stdout_snippet,
            stderr_snippet=stderr_snippet,
            checkpoint=checkpoint,
            policy_warnings=tuple(policy_warnings),
            read_only=_operation_is_read_only(operation) if read_only is None else read_only,
            cancellation_requested=cancellation_requested,
            cancellation_recorded=cancellation_recorded,
            metadata={} if metadata is None else metadata,
        ).redacted()

    @classmethod
    def unavailable(
        cls,
        *,
        adapter_id: str,
        operation: ExternalAgentOperation,
        reason: str,
        details: tuple[str, ...] | list[str] = (),
        commands_run: tuple[str, ...] | list[str] = (),
        tests_run: tuple[str, ...] | list[str] = (),
        artifacts: tuple[str, ...] | list[str] = (),
        stderr_snippet: str | None = None,
        policy_warnings: tuple[str, ...] | list[str] = (),
        cancellation_requested: bool = False,
        cancellation_recorded: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ExternalAgentRunResult:
        return cls(
            adapter_id=adapter_id,
            operation=operation,
            status=ExternalAgentRunStatus.UNAVAILABLE,
            summary=reason,
            details=tuple(details),
            commands_run=tuple(commands_run),
            tests_run=tuple(tests_run),
            artifacts=tuple(artifacts),
            stderr_snippet=stderr_snippet,
            policy_warnings=tuple(policy_warnings),
            read_only=_operation_is_read_only(operation),
            cancellation_requested=cancellation_requested,
            cancellation_recorded=cancellation_recorded,
            unavailable_reason=reason,
            metadata={} if metadata is None else metadata,
        ).redacted()

    @classmethod
    def cancelled(
        cls,
        *,
        adapter_id: str,
        operation: ExternalAgentOperation,
        reason: str,
        cancellation_requested: bool = True,
        cancellation_recorded: bool = True,
        checkpoint: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExternalAgentRunResult:
        return cls(
            adapter_id=adapter_id,
            operation=operation,
            status=ExternalAgentRunStatus.CANCELLED,
            summary=reason,
            checkpoint=checkpoint,
            read_only=_operation_is_read_only(operation),
            cancellation_requested=cancellation_requested,
            cancellation_recorded=cancellation_recorded,
            metadata={} if metadata is None else metadata,
        ).redacted()

    def redacted(self) -> ExternalAgentRunResult:
        return replace(
            self,
            summary=redact_secrets(self.summary),
            details=tuple(redact_secrets(detail) for detail in self.details),
            commands_run=tuple(redact_secrets(command) for command in self.commands_run),
            tests_run=tuple(redact_secrets(test) for test in self.tests_run),
            quota_estimate=redact_secrets(self.quota_estimate)
            if self.quota_estimate is not None
            else None,
            stdout_snippet=redact_secrets(self.stdout_snippet)
            if self.stdout_snippet is not None
            else None,
            stderr_snippet=redact_secrets(self.stderr_snippet)
            if self.stderr_snippet is not None
            else None,
            checkpoint=redact_secrets(self.checkpoint) if self.checkpoint is not None else None,
            policy_warnings=tuple(redact_secrets(warning) for warning in self.policy_warnings),
            unavailable_reason=redact_secrets(self.unavailable_reason)
            if self.unavailable_reason is not None
            else None,
            metadata=_redact_value(self.metadata),
        )


@dataclass(slots=True, frozen=True)
class ExternalAgentProbeSpec:
    """Probe request parameters.

    The contract layer keeps this intentionally small. It exists so probes can be
    extended later without changing the adapter protocol.
    """

    include_secrets: bool = False


@runtime_checkable
class ExternalAgentAdapterProtocol(Protocol):
    """Contract every external adapter must satisfy."""

    @property
    def adapter_id(self) -> str: ...

    @property
    def capabilities(self) -> ExternalAgentCapability: ...

    def probe(self, spec: ExternalAgentProbeSpec | None = None) -> ExternalAgentProbeResult: ...

    def run(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult: ...

    def cancel(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult: ...


class ExternalAgentAdapterBase(ABC):
    """Reusable base class for local contract validation."""

    adapter_id: str = ""
    capabilities: ExternalAgentCapability = ExternalAgentCapability()

    def probe(self, spec: ExternalAgentProbeSpec | None = None) -> ExternalAgentProbeResult:
        result = self._probe(spec or ExternalAgentProbeSpec())
        if not isinstance(result, ExternalAgentProbeResult):
            raise ExternalAgentAdapterError(
                f"{type(self).__name__}.probe() must return ExternalAgentProbeResult"
            )
        return result.redacted()

    def run(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult:
        validation = self._validate_context(context)
        if validation is not None:
            return validation
        result = self._run(context)
        if not isinstance(result, ExternalAgentRunResult):
            raise ExternalAgentAdapterError(
                f"{type(self).__name__}.run() must return ExternalAgentRunResult"
            )
        if self.capabilities.is_read_only(context.operation) and result.changed_files:
            return ExternalAgentRunResult.unavailable(
                adapter_id=self.adapter_id,
                operation=context.operation,
                reason=f"read-only operation reported changed files: {context.operation.value}",
                details=result.details,
                policy_warnings=(
                    "Read-only adapter operation returned changed_files and was rejected.",
                ),
                metadata=result.metadata,
            )
        if context.read_only and not result.read_only:
            result = replace(result, read_only=True)
        return result.redacted()

    def cancel(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult:
        return ExternalAgentRunResult.cancelled(
            adapter_id=self.adapter_id,
            operation=context.operation,
            reason="Cancellation requested; best-effort request recorded.",
            cancellation_requested=True,
            cancellation_recorded=True,
        )

    def _validate_context(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult | None:
        if not self.adapter_id.strip():
            raise ExternalAgentAdapterError(f"{type(self).__name__}.adapter_id cannot be empty")
        capability_adapter_id = self.capabilities.adapter_id
        if capability_adapter_id is not None and capability_adapter_id != self.adapter_id:
            raise ExternalAgentAdapterError(
                f"{type(self).__name__}.capabilities.adapter_id must match adapter_id"
            )
        if not context.prompt.strip() and context.task_id is None and context.work_item_id is None:
            return ExternalAgentRunResult.unavailable(
                adapter_id=self.adapter_id,
                operation=context.operation,
                reason="work item or task context required",
            )
        if context.adapter_id != self.adapter_id:
            return ExternalAgentRunResult.unavailable(
                adapter_id=self.adapter_id,
                operation=context.operation,
                reason=(
                    f"context adapter mismatch: expected {self.adapter_id}, got {context.adapter_id}"
                ),
            )
        if not self.capabilities.supports(context.operation):
            return ExternalAgentRunResult.unavailable(
                adapter_id=self.adapter_id,
                operation=context.operation,
                reason=f"unsupported operation: {context.operation.value}",
            )
        if self.capabilities.requires_workspace(context.operation) and context.workspace is None:
            return ExternalAgentRunResult.unavailable(
                adapter_id=self.adapter_id,
                operation=context.operation,
                reason="workspace assignment required for write-capable operations",
            )
        return None

    @abstractmethod
    def _probe(self, spec: ExternalAgentProbeSpec) -> ExternalAgentProbeResult: ...

    @abstractmethod
    def _run(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult: ...


@dataclass(slots=True, frozen=True)
class UnavailableExternalAgentAdapter(ExternalAgentAdapterBase):
    """Structured fallback returned when an adapter cannot be resolved."""

    adapter_id: str
    reason: str = "adapter unavailable"
    capabilities: ExternalAgentCapability = field(
        default_factory=lambda: ExternalAgentCapability(
            supported_operations=frozenset(),
            read_only_operations=frozenset(),
            write_operations=frozenset(),
            supports_cancellation=False,
        )
    )

    def _probe(self, spec: ExternalAgentProbeSpec) -> ExternalAgentProbeResult:
        _ = spec
        return ExternalAgentProbeResult(
            adapter_id=self.adapter_id,
            summary=redact_secrets(self.reason),
            details=(redact_secrets(self.reason),),
            available=False,
        )

    def run(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult:
        return ExternalAgentRunResult.unavailable(
            adapter_id=self.adapter_id,
            operation=context.operation,
            reason=redact_secrets(self.reason),
            cancellation_requested=context.cancellation_requested,
            cancellation_recorded=False,
        )

    def cancel(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult:
        return ExternalAgentRunResult.cancelled(
            adapter_id=self.adapter_id,
            operation=context.operation,
            reason=redact_secrets(self.reason),
            cancellation_requested=True,
            cancellation_recorded=True,
        )

    def _run(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult:
        _ = context
        return ExternalAgentRunResult.unavailable(
            adapter_id=self.adapter_id,
            operation=context.operation,
            reason=redact_secrets(self.reason),
        )


class ExternalAgentAdapterRegistry:
    """Local adapter registry keyed by adapter id."""

    def __init__(self) -> None:
        self._adapters: dict[str, ExternalAgentAdapterProtocol] = {}

    def register(self, adapter: ExternalAgentAdapterProtocol) -> None:
        if not adapter.adapter_id.strip():
            raise ExternalAgentAdapterError("Adapter id cannot be empty")
        capability_adapter_id = adapter.capabilities.adapter_id
        if capability_adapter_id is not None and capability_adapter_id != adapter.adapter_id:
            raise ExternalAgentAdapterError(
                f"Adapter '{adapter.adapter_id}' capability id does not match"
            )
        if adapter.adapter_id in self._adapters:
            raise ExternalAgentAdapterError(f"Adapter '{adapter.adapter_id}' already registered")
        self._adapters[adapter.adapter_id] = adapter

    def resolve(self, adapter_id: str) -> ExternalAgentAdapterProtocol:
        adapter = self._adapters.get(adapter_id)
        if adapter is not None:
            return adapter
        return UnavailableExternalAgentAdapter(
            adapter_id=adapter_id,
            reason=f"adapter '{adapter_id}' is unavailable",
        )

    def available(self) -> list[str]:
        return sorted(self._adapters)


_READ_ONLY_EXTERNAL_OPERATIONS = frozenset(
    {
        ExternalAgentOperation.PROBE,
        ExternalAgentOperation.PLAN,
        ExternalAgentOperation.REVIEW,
        ExternalAgentOperation.VALIDATE,
        ExternalAgentOperation.CHECKPOINT,
        ExternalAgentOperation.CANCEL,
        ExternalAgentOperation.READ,
    }
)


class BackendReadOnlyExternalAgentAdapter(ExternalAgentAdapterBase):
    """Expose an existing ``ILLMBackend`` as a read-only external-agent adapter."""

    def __init__(
        self,
        *,
        backend: ILLMBackend,
        adapter_id: str,
        display_name: str,
        model: str,
        command_hint: str,
        version: str | None = None,
        probe_info: tuple[str, ...] = (),
        supported_roles: frozenset[str] | None = None,
        capability_tags: frozenset[str] | None = None,
        cost_model: str | None = None,
        quota_model: str | None = None,
        required_credentials: tuple[str, ...] = (),
        required_binaries: tuple[str, ...] = (),
        workspace_requirements: tuple[str, ...] = ("workspace optional for read-only runs",),
        safety_notes: tuple[str, ...] = (),
    ) -> None:
        if not adapter_id.strip():
            raise ExternalAgentAdapterError("Adapter id cannot be empty")
        self.adapter_id = adapter_id
        self._backend = backend
        self._model = model
        self._version = version
        self._command_hint = command_hint
        backend_caps = self._backend.capabilities(model)
        self.capabilities = ExternalAgentCapability(
            adapter_id=adapter_id,
            display_name=display_name,
            version=version,
            probe_info=probe_info,
            supported_roles=supported_roles
            or frozenset({"planner", "reviewer", "validator", "checkpoint"}),
            supported_operations=_READ_ONLY_EXTERNAL_OPERATIONS,
            read_only_operations=frozenset(
                operation
                for operation in _READ_ONLY_EXTERNAL_OPERATIONS
                if operation is not ExternalAgentOperation.CANCEL
            ),
            write_operations=frozenset(),
            capability_tags=capability_tags or frozenset({"cli", "llm", "non-interactive"}),
            context_limits={"max_context_tokens": backend_caps.max_context_tokens},
            cost_model=cost_model,
            quota_model=quota_model,
            required_credentials=required_credentials,
            required_binaries=required_binaries,
            workspace_requirements=workspace_requirements,
            can_edit_files=False,
            can_run_tests=False,
            supports_background=True,
            supports_cancellation=True,
            safety_notes=(
                *safety_notes,
                "Read-only wrapper must not edit files or merge PRs.",
                "Gate decisions, merges, and policy approvals remain outside the adapter.",
            ),
        )

    def _probe(self, spec: ExternalAgentProbeSpec) -> ExternalAgentProbeResult:
        _ = spec
        try:
            available = _run_coroutine_sync(self._backend.health_check())
        except BackendUnavailableError as exc:
            return ExternalAgentProbeResult(
                adapter_id=self.adapter_id,
                summary=f"{self.capabilities.display_name} backend unavailable: {exc}",
                version=self._version,
                details=(self._command_hint,),
                metadata={"backend": self._backend.name, "model": self._model},
                available=False,
            )
        summary = (
            f"{self.capabilities.display_name} backend available"
            if available
            else f"{self.capabilities.display_name} backend unavailable"
        )
        return ExternalAgentProbeResult(
            adapter_id=self.adapter_id,
            summary=summary,
            version=self._version,
            details=(self._command_hint,),
            metadata={"backend": self._backend.name, "model": self._model},
            available=available,
        )

    def _run(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult:
        if context.operation is ExternalAgentOperation.CANCEL:
            return self.cancel(context)
        if context.operation is ExternalAgentOperation.PROBE:
            probe = self.probe()
            return ExternalAgentRunResult.completed(
                adapter_id=self.adapter_id,
                operation=context.operation,
                summary=probe.summary,
                details=probe.details,
                read_only=True,
                metadata=probe.metadata,
            )

        try:
            response = _run_coroutine_sync(
                self._backend.complete(
                    self._messages_for(context),
                    model=self._model,
                    temperature=0.0,
                )
            )
        except BackendUnavailableError as exc:
            return ExternalAgentRunResult.unavailable(
                adapter_id=self.adapter_id,
                operation=context.operation,
                reason=f"{self.capabilities.display_name} backend unavailable: {exc}",
                commands_run=(self._command_hint,),
                stderr_snippet=str(exc),
            )

        return self._result_from_response(context, response)

    def _messages_for(self, context: ExternalAgentRunContext) -> list[Message]:
        return [
            Message(role=MessageRole.SYSTEM, content=self._system_prompt_for(context.operation)),
            Message(role=MessageRole.USER, content=context.prompt),
        ]

    def _system_prompt_for(self, operation: ExternalAgentOperation) -> str:
        instructions = {
            ExternalAgentOperation.PLAN: "Produce a plan only. Do not edit files.",
            ExternalAgentOperation.REVIEW: (
                "Review the provided context and return structured findings. Do not edit files."
            ),
            ExternalAgentOperation.VALIDATE: (
                "Analyze or propose validation commands. Do not edit files."
            ),
            ExternalAgentOperation.CHECKPOINT: (
                "Summarize recoverable state, decisions, blockers, and next steps. Do not edit files."
            ),
            ExternalAgentOperation.READ: "Inspect and summarize the requested context. Do not edit files.",
        }
        return instructions.get(operation, "Respond without editing files.")

    def _result_from_response(
        self, context: ExternalAgentRunContext, response: BackendResponse
    ) -> ExternalAgentRunResult:
        return ExternalAgentRunResult.completed(
            adapter_id=self.adapter_id,
            operation=context.operation,
            summary=response.content,
            details=(f"finish_reason={response.finish_reason}",),
            commands_run=(self._command_hint,),
            stdout_snippet=_snippet(response.content),
            checkpoint=response.content
            if context.operation is ExternalAgentOperation.CHECKPOINT
            else None,
            read_only=True,
            cost_estimate_usd=self._backend.estimate_cost(response.usage, self._model),
            quota_estimate=self.capabilities.quota_model,
            metadata={
                "backend": response.backend,
                "model": response.model,
                "raw": response.raw,
            },
        )


class CodexCLIExternalAgentAdapter(BackendReadOnlyExternalAgentAdapter):
    """Expose the existing Codex CLI backend through the external-agent contract."""

    def __init__(
        self,
        *,
        backend: ILLMBackend | None = None,
        model: str = "gpt-5-codex",
        adapter_id: str = "codex-cli",
        version: str | None = None,
        command_hint: str | None = None,
    ) -> None:
        super().__init__(
            backend=backend if backend is not None else CodexCLIBackend(approval="suggest"),
            adapter_id=adapter_id,
            display_name="Codex CLI",
            model=model,
            version=version,
            command_hint=command_hint or f"codex exec --approval suggest --model {model}",
            probe_info=("codex --version",),
            capability_tags=frozenset({"cli", "codex", "llm", "non-interactive"}),
            cost_model="uses the caller's Codex/OpenAI account; exact cost may be unavailable",
            quota_model="delegated to Codex CLI authentication and provider quota",
            required_credentials=("codex login or equivalent CLI authentication",),
            required_binaries=("codex",),
            workspace_requirements=("workspace optional for read-only suggest-mode runs",),
            safety_notes=("Default wrapper uses Codex CLI suggest mode and must not edit files.",),
        )


class ContinueCLIExternalAgentAdapter(BackendReadOnlyExternalAgentAdapter):
    """Expose Continue's ``cn`` CLI backend as a read-only external agent."""

    def __init__(
        self,
        *,
        backend: ILLMBackend | None = None,
        model: str = "continue-cli-config",
        adapter_id: str = "continue-cli",
        version: str | None = None,
        command_hint: str | None = None,
    ) -> None:
        super().__init__(
            backend=backend if backend is not None else ContinueCLIBackend(),
            adapter_id=adapter_id,
            display_name="Continue CLI",
            model=model,
            version=version,
            command_hint=command_hint or "cn ask <prompt>",
            probe_info=("cn --version",),
            capability_tags=frozenset({"cli", "continue", "llm", "non-interactive"}),
            cost_model="delegated to Continue configuration and selected model provider",
            quota_model="delegated to Continue assistant configuration",
            required_credentials=("Continue CLI configured with a local assistant",),
            required_binaries=("cn",),
            safety_notes=("Continue model/provider choice is owned by local Continue config.",),
        )


class ClaudeCodeCLIExternalAgentAdapter(BackendReadOnlyExternalAgentAdapter):
    """Expose Claude Code CLI as a read-only external agent."""

    def __init__(
        self,
        *,
        backend: ILLMBackend | None = None,
        model: str = "claude-sonnet-4-6",
        adapter_id: str = "claude-code-cli",
        version: str | None = None,
        command_hint: str | None = None,
    ) -> None:
        super().__init__(
            backend=backend if backend is not None else ClaudeCodeCLIBackend(),
            adapter_id=adapter_id,
            display_name="Claude Code CLI",
            model=model,
            version=version,
            command_hint=command_hint or f"claude -p <prompt> --model {model} --output-format json",
            probe_info=("claude --version",),
            capability_tags=frozenset({"cli", "claude", "llm", "vision", "non-interactive"}),
            cost_model="uses the caller's Claude Code subscription or Anthropic account",
            quota_model="delegated to Claude Code authentication and provider quota",
            required_credentials=("claude login or equivalent CLI authentication",),
            required_binaries=("claude",),
            safety_notes=(
                "Read-only wrapper uses prompt mode and must not grant write authority.",
            ),
        )


class JulesCLIExternalAgentAdapter(BackendReadOnlyExternalAgentAdapter):
    """Expose Jules CLI as a read-only external agent."""

    def __init__(
        self,
        *,
        backend: ILLMBackend | None = None,
        model: str = "jules-cli-default",
        adapter_id: str = "jules-cli",
        version: str | None = None,
        command_hint: str | None = None,
    ) -> None:
        super().__init__(
            backend=backend if backend is not None else JulesCLIBackend(),
            adapter_id=adapter_id,
            display_name="Jules CLI",
            model=model,
            version=version,
            command_hint=command_hint or "jules ask <prompt> --output-format json",
            probe_info=("jules --version",),
            capability_tags=frozenset({"cli", "jules", "llm", "non-interactive"}),
            cost_model="uses the caller's Jules account or local CLI configuration",
            quota_model="delegated to Jules CLI authentication and provider quota",
            required_credentials=("jules CLI authentication",),
            required_binaries=("jules",),
            safety_notes=("Jules wrapper is read-only until write-capable policy exists.",),
        )
