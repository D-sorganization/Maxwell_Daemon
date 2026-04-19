"""Backend-agnostic interface contract for LLM adapters.

Every concrete backend (Claude, OpenAI, Ollama, Google, Azure, Continue) implements
`ILLMBackend`. Callers code against this interface so swapping providers is a config
change, not a code change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(slots=True)
class Message:
    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )


@dataclass(slots=True)
class BackendCapabilities:
    """Declares what a backend can do. Used for routing and UI hints."""

    supports_streaming: bool = True
    supports_tool_use: bool = False
    supports_vision: bool = False
    supports_system_prompt: bool = True
    max_context_tokens: int = 8_192
    is_local: bool = False
    cost_per_1k_input_tokens: float = 0.0
    cost_per_1k_output_tokens: float = 0.0


@dataclass(slots=True)
class BackendResponse:
    content: str
    finish_reason: str
    usage: TokenUsage
    model: str
    backend: str
    raw: dict[str, Any] = field(default_factory=dict)


class BackendError(Exception):
    """Raised when a backend fails in a recoverable way."""


class BackendUnavailableError(BackendError):
    """Raised when a backend is unreachable (network, auth, quota)."""


class ILLMBackend(ABC):
    """Interface every LLM adapter implements.

    Concrete backends live in `conductor.backends.{name}` and are registered via
    the `registry` so they can be constructed from config.
    """

    #: Unique identifier used in config (`backend: "claude"`).
    name: str = ""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float = 1.0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> BackendResponse: ...

    @abstractmethod
    def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float = 1.0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Return an async iterator of content chunks.

        Concrete implementations are ``async def`` with ``yield`` (async generators),
        whose return type matches ``AsyncIterator[str]`` directly — no extra await
        needed at the call site.
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the backend is reachable and authenticated."""

    @abstractmethod
    def capabilities(self, model: str) -> BackendCapabilities:
        """Describe what `model` can do. Used for routing decisions."""

    def estimate_cost(self, usage: TokenUsage, model: str) -> float:
        caps = self.capabilities(model)
        return (
            usage.prompt_tokens * caps.cost_per_1k_input_tokens / 1000
            + usage.completion_tokens * caps.cost_per_1k_output_tokens / 1000
        )
