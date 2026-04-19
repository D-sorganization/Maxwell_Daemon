"""Backend router — chooses which backend/model handles each request.

Routing rules (in priority order):
1. Explicit `backend=` on the call.
2. Repo-level override from config.
3. Budget-aware fallback: if the configured backend is over budget, route to the
   cheapest available backend (typically Ollama, which costs nothing).
4. Global default from `agent.default_backend`.
"""

from __future__ import annotations

from dataclasses import dataclass

from conductor.backends import ILLMBackend, registry
from conductor.config import BackendConfig, ConductorConfig


@dataclass(slots=True)
class RouteDecision:
    backend: ILLMBackend
    backend_name: str
    model: str
    reason: str


class BackendRouter:
    def __init__(self, config: ConductorConfig) -> None:
        self._config = config
        self._instances: dict[str, ILLMBackend] = {}

    def _get_or_create(self, name: str, cfg: BackendConfig) -> ILLMBackend:
        if name not in self._instances:
            # Exclude `api_key` from model_dump() — SecretStr serialises to the
            # masked form, and we want to pass the raw value explicitly.
            params = cfg.model_dump(
                exclude={"type", "enabled", "model", "api_key"}, exclude_none=True
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
    ) -> RouteDecision:
        chosen, reason = self._choose_name(repo, backend_override)
        cfg = self._config.backends[chosen]
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

    def _choose_name(self, repo: str | None, override: str | None) -> tuple[str, str]:
        if override:
            if override not in self._config.backends:
                raise ValueError(f"Unknown backend override: {override}")
            return override, f"explicit override: {override}"

        if repo:
            repo_cfg = next((r for r in self._config.repos if r.name == repo), None)
            if repo_cfg and repo_cfg.backend:
                return repo_cfg.backend, f"repo override for {repo}"

        return self._config.agent.default_backend, "global default"

    def available_backends(self) -> list[str]:
        return [name for name, cfg in self._config.backends.items() if cfg.enabled]
