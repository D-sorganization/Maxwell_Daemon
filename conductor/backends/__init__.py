"""LLM backend abstraction layer and adapter implementations."""

from conductor.backends.base import (
    BackendCapabilities,
    BackendError,
    BackendResponse,
    BackendUnavailableError,
    ILLMBackend,
    Message,
    MessageRole,
    TokenUsage,
)
from conductor.backends.registry import BackendRegistry, registry

__all__ = [
    "BackendCapabilities",
    "BackendError",
    "BackendRegistry",
    "BackendResponse",
    "BackendUnavailableError",
    "ILLMBackend",
    "Message",
    "MessageRole",
    "TokenUsage",
    "registry",
]

# Ensure agent-loop backend is registered when this package is imported.
try:
    import conductor.backends.agent_loop as _agent_loop_mod  # noqa: F401
except ImportError:
    pass
