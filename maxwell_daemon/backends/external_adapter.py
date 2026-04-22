"""Contract layer for external agent adapters.

This module intentionally stays below policy, routing, and scheduling. It only
describes the shape of an external agent adapter, validates a few local
contract rules, and provides a structured unavailable fallback.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
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
    "UnavailableExternalAgentAdapter",
    "redact_secrets",
]


class ExternalAgentAdapterError(RuntimeError):
    """Raised when adapter registration or local contract validation fails."""


class ExternalAgentOperation(str, Enum):
    """Supported external-agent operations."""

    PROBE = "probe"
    READ = "read"
    WRITE = "write"
    REVIEW = "review"


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

    supported_operations: frozenset[ExternalAgentOperation] = field(
        default_factory=lambda: frozenset(ExternalAgentOperation)
    )
    read_only_operations: frozenset[ExternalAgentOperation] = field(
        default_factory=lambda: frozenset({ExternalAgentOperation.REVIEW})
    )
    write_operations: frozenset[ExternalAgentOperation] = field(
        default_factory=lambda: frozenset({ExternalAgentOperation.WRITE})
    )
    supports_cancellation: bool = True

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
    request_id: str | None = None
    cancellation_requested: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        return self.operation is ExternalAgentOperation.REVIEW

    @property
    def has_workspace(self) -> bool:
        return self.workspace is not None


@dataclass(slots=True, frozen=True)
class ExternalAgentProbeResult:
    """Sanitized adapter diagnostics."""

    adapter_id: str
    summary: str
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
    artifacts: tuple[str, ...] = ()
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
        artifacts: tuple[str, ...] | list[str] = (),
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
            artifacts=tuple(artifacts),
            read_only=operation is ExternalAgentOperation.REVIEW
            if read_only is None
            else read_only,
            cancellation_requested=cancellation_requested,
            cancellation_recorded=cancellation_recorded,
            metadata={} if metadata is None else metadata,
        )

    @classmethod
    def unavailable(
        cls,
        *,
        adapter_id: str,
        operation: ExternalAgentOperation,
        reason: str,
        details: tuple[str, ...] | list[str] = (),
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
            read_only=operation is ExternalAgentOperation.REVIEW,
            cancellation_requested=cancellation_requested,
            cancellation_recorded=cancellation_recorded,
            unavailable_reason=reason,
            metadata={} if metadata is None else metadata,
        )

    @classmethod
    def cancelled(
        cls,
        *,
        adapter_id: str,
        operation: ExternalAgentOperation,
        reason: str,
        cancellation_requested: bool = True,
        cancellation_recorded: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> ExternalAgentRunResult:
        return cls(
            adapter_id=adapter_id,
            operation=operation,
            status=ExternalAgentRunStatus.CANCELLED,
            summary=reason,
            read_only=operation is ExternalAgentOperation.REVIEW,
            cancellation_requested=cancellation_requested,
            cancellation_recorded=cancellation_recorded,
            metadata={} if metadata is None else metadata,
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
        if context.read_only and not result.read_only:
            result = replace(result, read_only=True)
        return result

    def cancel(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult:
        return ExternalAgentRunResult.cancelled(
            adapter_id=self.adapter_id,
            operation=context.operation,
            reason="Cancellation requested; best-effort request recorded.",
            cancellation_requested=True,
            cancellation_recorded=True,
        )

    def _validate_context(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult | None:
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
                reason="workspace assignment required for write operations",
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
