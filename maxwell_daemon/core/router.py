"""Backend router — chooses which backend/model handles each request.

Routing rules (in priority order):
1. Explicit `backend=` on the call.
2. Repo-level override from config.
3. Budget-aware fallback: if the configured backend is over budget, route to the
   cheapest available backend (typically Ollama, which costs nothing).
4. Global default from `agent.default_backend`.
"""

from __future__ import annotations

import logging
from maxwell_daemon.logging import get_logger
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from maxwell_daemon.backends import ILLMBackend
from maxwell_daemon.backends.registry import registry
from maxwell_daemon.config import BackendConfig, MaxwellDaemonConfig

if TYPE_CHECKING:
    pass

log = get_logger(__name__)


@dataclass(slots=True)
class RouteDecision:
    backend: ILLMBackend
    backend_name: str
    model: str
    reason: str


class BackendRouter:
    def __init__(self, config: MaxwellDaemonConfig, budget: Any = None) -> None:
        self._config = config
        self._budget = budget
        self._instances: dict[str, ILLMBackend] = {}

    def _get_or_create(self, name: str, cfg: BackendConfig) -> ILLMBackend:
        if name not in self._instances:
            # Exclude `api_key` from model_dump() — SecretStr serialises to the
            # masked form, and we want to pass the raw value explicitly.
            # Also exclude routing-only fields that backend adapters don't understand.
            params = cfg.model_dump(
                exclude={
                    "type",
                    "enabled",
                    "model",
                    "api_key",
                    "fallback_backend",
                    "fallback_threshold_percent",
                },
                exclude_none=True,
            )
            if (key := cfg.api_key_value()) is not None:
                params["api_key"] = key
            self._instances[name] = registry.create(cfg.type, params)
        return self._instances[name]

    def route(
        self,
        *,
        repo: str | None = None,
        backend_override: str | None = None,
        model_override: str | None = None,
        budget_percent: float | None = None,
    ) -> RouteDecision:
        chosen, reason = self._choose_name(repo, backend_override, budget_percent)
        cfg = self._all_backend_configs()[chosen]
        if not cfg.enabled:
            raise RuntimeError(f"Backend '{chosen}' is disabled in config")

        repo_cfg = next((r for r in self._config.repos if r.name == repo), None)
        model = (
            model_override
            or (repo_cfg.model if repo_cfg and repo_cfg.backend == chosen else None)
            or cfg.model
        )
        return RouteDecision(
            backend=self._get_or_create(chosen, cfg),
            backend_name=chosen,
            model=model,
            reason=reason,
        )

    def _choose_name(
        self,
        repo: str | None,
        override: str | None,
        budget_percent: float | None = None,
    ) -> tuple[str, str]:
        if override:
            if override not in self._all_backend_configs():
                raise ValueError(f"Unknown backend override: {override}")
            return override, f"explicit override: {override}"

        if repo:
            repo_cfg = next((r for r in self._config.repos if r.name == repo), None)
            if repo_cfg and repo_cfg.backend:
                candidate = repo_cfg.backend
                return self._apply_budget_fallback(
                    candidate, f"repo override for {repo}", budget_percent
                )

        candidate = self._default_backend_name()
        return self._apply_budget_fallback(candidate, "global default", budget_percent)

    def _apply_budget_fallback(
        self,
        candidate: str,
        reason: str,
        budget_percent: float | None,
    ) -> tuple[str, str]:
        """Return (name, reason), switching to fallback_backend if over budget threshold."""
        if budget_percent is None:
            return candidate, reason

        cfg = self._backend_config(candidate)
        if cfg is None:
            return candidate, reason

        if cfg.fallback_backend is not None and budget_percent >= cfg.fallback_threshold_percent:
            fallback = cfg.fallback_backend
            fallback_reason = (
                f"budget fallback from {candidate} to {fallback} "
                f"(spend {budget_percent:.1f}% >= threshold {cfg.fallback_threshold_percent:.1f}%)"
            )
            return fallback, fallback_reason

        return candidate, reason

    def available_backends(self) -> list[str]:
        return [name for name, cfg in self._all_backend_configs().items() if cfg.enabled]

    # ── Config boundary accessors ─────────────────────────────────────────────
    # These methods prevent callers from traversing the config object graph
    # directly (Law of Demeter).  All config-graph traversal is centralised here.

    def _default_backend_name(self) -> str:
        """Return the configured default backend name."""
        return self._config.agent.default_backend

    def _backend_config(self, name: str) -> BackendConfig | None:
        """Return the BackendConfig for *name*, or None if unknown."""
        return self._config.backends.get(name)

    def _all_backend_configs(self) -> dict[str, BackendConfig]:
        """Return all backend configs keyed by name."""
        return self._config.backends

    async def aclose_all(self) -> None:
        """Close all instantiated backends."""
        for backend in self._instances.values():
            close_method = getattr(backend, "aclose", None)
            if close_method is not None:
                import asyncio
                import contextlib

                with contextlib.suppress(Exception):
                    res = close_method()
                    if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                        await res
