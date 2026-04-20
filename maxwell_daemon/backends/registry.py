"""Backend registry — the factory that wires config names to concrete adapters.

Autoload is lazy and best-effort: if a backend's SDK isn't installed, that adapter
just won't register, and requests targeting it fail with a clear error. This lets
people install only the extras they need (e.g., `pip install maxwell-daemon[ollama]`).
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from maxwell_daemon.backends.base import BackendError, ILLMBackend

log = logging.getLogger(__name__)

_BUILTIN_BACKENDS = ("claude", "openai", "azure", "ollama", "claude_code", "agent_loop")


class BackendRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, type[ILLMBackend]] = {}

    def register(self, name: str, backend_cls: type[ILLMBackend]) -> None:
        if name in self._factories:
            raise BackendError(f"Backend '{name}' already registered")
        self._factories[name] = backend_cls

    def create(self, name: str, config: dict[str, Any]) -> ILLMBackend:
        if name not in self._factories:
            raise BackendError(
                f"Unknown backend '{name}'. Registered: {sorted(self._factories)}. "
                f"Missing SDK? Try: pip install maxwell-daemon[{name}]"
            )
        return self._factories[name](**config)

    def available(self) -> list[str]:
        return sorted(self._factories)


registry = BackendRegistry()


def _autoload() -> None:
    for name in _BUILTIN_BACKENDS:
        try:
            importlib.import_module(f"maxwell_daemon.backends.{name}")
        except ImportError as e:
            log.debug("backend %r not loaded (missing SDK): %s", name, e)


_autoload()
