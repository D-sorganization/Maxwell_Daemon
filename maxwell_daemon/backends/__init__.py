"""LLM backend abstraction layer and adapter implementations."""

from contextlib import suppress
from importlib import import_module

from maxwell_daemon.backends.base import (
    BackendCapabilities,
    BackendError,
    BackendResponse,
    BackendUnavailableError,
    ILLMBackend,
    Message,
    MessageRole,
    TokenUsage,
)
from maxwell_daemon.backends.registry import BackendRegistry, registry

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
with suppress(ImportError):
    import_module("maxwell_daemon.backends.agent_loop")
