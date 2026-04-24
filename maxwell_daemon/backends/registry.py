"""Backend registry — the factory that wires config names to concrete adapters.

Autoload is lazy and best-effort: if a backend's SDK isn't installed, that adapter
just won't register, and requests targeting it fail with a clear error. This lets
people install only the extras they need (e.g., `pip install maxwell-daemon[ollama]`).
"""

from __future__ import annotations

import importlib
import logging
from maxwell_daemon.logging import get_logger
from dataclasses import dataclass
from typing import Any

from maxwell_daemon.backends.base import BackendError, ILLMBackend

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BackendManifest:
    module_name: str | None
    name: str
    display_name: str
    description: str
    requires_api_key: bool
    local_only: bool
    default_endpoint: str | None = None
    api_key_env_var: str | None = None
    endpoint_env_var: str | None = None
    install_extra: str | None = None
    command: str | None = None


_BUILTIN_MANIFESTS = (
    BackendManifest(
        module_name="claude",
        name="claude",
        display_name="Anthropic Claude",
        description="Anthropic-hosted Claude models over the public API.",
        requires_api_key=True,
        local_only=False,
        api_key_env_var="ANTHROPIC_API_KEY",
    ),
    BackendManifest(
        module_name="openai",
        name="openai",
        display_name="OpenAI",
        description="OpenAI-hosted models or OpenAI-compatible endpoints.",
        requires_api_key=True,
        local_only=False,
        api_key_env_var="OPENAI_API_KEY",
    ),
    BackendManifest(
        module_name="azure",
        name="azure",
        display_name="Azure OpenAI",
        description="Azure OpenAI deployments with endpoint and key configuration.",
        requires_api_key=True,
        local_only=False,
        api_key_env_var="AZURE_OPENAI_API_KEY",
        endpoint_env_var="AZURE_OPENAI_ENDPOINT",
        install_extra="azure",
    ),
    BackendManifest(
        module_name="ollama",
        name="ollama",
        display_name="Ollama",
        description="Local Ollama inference over the default localhost HTTP API.",
        requires_api_key=False,
        local_only=True,
        default_endpoint="http://localhost:11434",
        endpoint_env_var="OLLAMA_HOST",
        install_extra="ollama",
    ),
    BackendManifest(
        module_name="claude_code",
        name="claude-code-cli",
        display_name="Claude Code CLI",
        description="Local Claude Code executable using the caller's existing Claude account.",
        requires_api_key=False,
        local_only=False,
        command="claude",
    ),
    BackendManifest(
        module_name="codex_cli",
        name="codex-cli",
        display_name="Codex CLI",
        description="Local Codex executable using the caller's existing OpenAI account.",
        requires_api_key=False,
        local_only=False,
        command="codex",
    ),
    BackendManifest(
        module_name="continue_cli",
        name="continue-cli",
        display_name="Continue CLI",
        description="Local Continue executable wired through the user's existing Continue setup.",
        requires_api_key=False,
        local_only=False,
        command="continue",
    ),
    BackendManifest(
        module_name="jules_cli",
        name="jules-cli",
        display_name="Jules CLI",
        description="Local Jules executable using the caller's existing Jules installation.",
        requires_api_key=False,
        local_only=False,
        command="jules",
    ),
    BackendManifest(
        module_name="agent_loop",
        name="agent-loop",
        display_name="Anthropic Agent Loop",
        description="Anthropic tool-use loop backend for multi-step autonomous work.",
        requires_api_key=True,
        local_only=False,
        api_key_env_var="ANTHROPIC_API_KEY",
    ),
)
_BUILTIN_MANIFESTS_BY_NAME = {manifest.name: manifest for manifest in _BUILTIN_MANIFESTS}


def _display_name_for_runtime_backend(name: str) -> str:
    display = name.replace("-", " ").replace("_", " ").title()
    return display.replace("Api", "API").replace("Cli", "CLI").replace("Mcp", "MCP")


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

    def catalog(self) -> list[BackendManifest]:
        manifests = list(_BUILTIN_MANIFESTS)
        for name in sorted(self._factories):
            if name in _BUILTIN_MANIFESTS_BY_NAME:
                continue
            manifests.append(
                BackendManifest(
                    module_name=None,
                    name=name,
                    display_name=_display_name_for_runtime_backend(name),
                    description="Runtime-registered backend.",
                    requires_api_key=False,
                    local_only=False,
                )
            )
        return manifests


registry = BackendRegistry()


def _autoload() -> None:
    for manifest in _BUILTIN_MANIFESTS:
        if manifest.module_name is None:
            continue
        try:
            importlib.import_module(f"maxwell_daemon.backends.{manifest.module_name}")
        except ImportError as e:
            log.debug("backend %r not loaded (missing SDK): %s", manifest.name, e)


_autoload()
