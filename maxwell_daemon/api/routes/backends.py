"""Backend discovery and configuration endpoints.

Extracted from ``maxwell_daemon/api/server.py`` as part of epic #896
Phase 1.1.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from maxwell_daemon.backends import BackendManifest, registry
from maxwell_daemon.config.loader import _default_secret_store, save_config
from maxwell_daemon.config.models import BackendConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "AvailableBackendView",
    "BackendCatalogEntryView",
    "BackendCatalogResponse",
    "BackendConfigPayload",
    "BackendTestResponse",
    "OnboardingSmokeTestRequest",
    "register",
]


class BackendConfigPayload(BaseModel):
    name: str
    api_key: str | None = None
    endpoint: str | None = None
    set_as_default: bool = False


class AvailableBackendView(BaseModel):
    name: str
    display_name: str
    description: str
    requires_api_key: bool
    local_only: bool
    logo_key: str | None = None
    default_endpoint: str | None = None
    configured: bool = False


class BackendCatalogEntryView(BaseModel):
    name: str
    display_name: str
    description: str
    requires_api_key: bool
    local_only: bool
    logo_key: str | None = None
    default_endpoint: str | None = None
    api_key_env_var: str | None = None
    endpoint_env_var: str | None = None
    install_extra: str | None = None
    command: str | None = None
    configured_aliases: tuple[str, ...] = ()
    loaded: bool
    connected: bool

    @classmethod
    def from_manifest(
        cls,
        manifest: BackendManifest,
        *,
        configured_aliases: tuple[str, ...],
        loaded: bool,
        connected: bool,
    ) -> BackendCatalogEntryView:
        return cls(
            name=manifest.name,
            display_name=manifest.display_name,
            description=manifest.description,
            requires_api_key=manifest.requires_api_key,
            local_only=manifest.local_only,
            logo_key=manifest.logo_key,
            default_endpoint=manifest.default_endpoint,
            api_key_env_var=manifest.api_key_env_var,
            endpoint_env_var=manifest.endpoint_env_var,
            install_extra=manifest.install_extra,
            command=manifest.command,
            configured_aliases=configured_aliases,
            loaded=loaded,
            connected=connected,
        )


class BackendCatalogResponse(BaseModel):
    backends: tuple[BackendCatalogEntryView, ...]


class BackendTestResponse(BaseModel):
    success: bool
    latency_ms: float | None = None
    models: list[str] = Field(default_factory=list)
    error: str | None = None


class OnboardingSmokeTestRequest(BaseModel):
    backend_name: str
    model: str


def register(  # noqa: C901
    app: FastAPI,
    daemon: Daemon,
    require_viewer: Any,
    require_admin: Any,
) -> None:
    """Attach backend management endpoints to ``app``."""

    @app.get("/api/v1/backends", dependencies=[Depends(require_viewer)])
    async def list_backends() -> dict[str, Any]:
        return {"backends": daemon.state().backends_available}

    @app.get(
        "/api/v1/backends/available",
        dependencies=[Depends(require_viewer)],
        response_model=BackendCatalogResponse,
    )
    async def list_available_backends() -> BackendCatalogResponse:
        configured_aliases_by_type: dict[str, list[str]] = {}
        for alias, backend_cfg in daemon._config.backends.items():
            configured_aliases_by_type.setdefault(backend_cfg.type, []).append(alias)

        connected_aliases = set(daemon.state().backends_available)
        loaded_backend_types = set(registry.available())
        catalog = tuple(
            BackendCatalogEntryView.from_manifest(
                manifest,
                configured_aliases=tuple(sorted(configured_aliases_by_type.get(manifest.name, ()))),
                loaded=manifest.name in loaded_backend_types,
                connected=any(
                    alias in connected_aliases
                    for alias in configured_aliases_by_type.get(manifest.name, ())
                ),
            )
            for manifest in registry.catalog()
        )
        return BackendCatalogResponse(backends=catalog)

    @app.post("/api/v1/backends", dependencies=[Depends(require_admin)])
    async def configure_backend(payload: BackendConfigPayload) -> dict[str, Any]:
        from maxwell_daemon.secrets import backend_api_key_secret_ref

        manifest = next((m for m in registry.catalog() if m.name == payload.name), None)
        if not manifest:
            raise HTTPException(
                status_code=404,
                detail=f"Backend '{payload.name}' not found in registry",
            )

        store = _default_secret_store()
        secret_ref = backend_api_key_secret_ref(payload.name)
        if payload.api_key and store:
            store.set(secret_ref, payload.api_key)

        cfg_dict: dict[str, Any] = {
            "type": payload.name,
            "model": "",
            "base_url": payload.endpoint or manifest.default_endpoint,
            "api_key_secret_ref": secret_ref if (payload.api_key and store) else None,
        }

        if payload.name == "ollama":
            cfg_dict["model"] = "llama3"
        elif payload.name == "openai":
            cfg_dict["model"] = "gpt-4o-mini"
        elif payload.name == "claude":
            cfg_dict["model"] = "claude-haiku-4-5"
        else:
            cfg_dict["model"] = "default"

        daemon._config.backends[payload.name] = BackendConfig(**cfg_dict)

        if payload.set_as_default:
            daemon._config.agent.default_backend = payload.name

        try:
            save_config(daemon._config)
            daemon._router._get_or_create(payload.name, daemon._config.backends[payload.name])
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to save config: {e}") from e

        return {"status": "success"}

    @app.post("/api/v1/backends/{name}/test", dependencies=[Depends(require_admin)])
    async def test_backend(name: str) -> BackendTestResponse:
        import contextlib
        from time import monotonic

        if name in daemon._router._instances:
            backend = daemon._router._instances[name]
        else:
            if name not in daemon._config.backends:
                raise HTTPException(status_code=404, detail=f"Backend '{name}' not configured")
            try:
                backend = daemon._router._get_or_create(name, daemon._config.backends[name])
            except Exception as e:  # noqa: BLE001
                return BackendTestResponse(success=False, error=str(e))

        start = monotonic()
        try:
            is_healthy = await backend.health_check()
        except Exception as e:  # noqa: BLE001
            return BackendTestResponse(success=False, error=str(e))

        latency = (monotonic() - start) * 1000

        models = []
        if is_healthy and hasattr(backend, "list_models"):
            with contextlib.suppress(Exception):
                models = await backend.list_models()

        return BackendTestResponse(
            success=is_healthy,
            latency_ms=latency,
            models=models,
            error=None if is_healthy else "Health check returned false",
        )

    @app.post("/api/v1/onboarding/smoke-test", dependencies=[Depends(require_admin)])
    async def onboarding_smoke_test(
        payload: OnboardingSmokeTestRequest,
    ) -> dict[str, Any]:
        from maxwell_daemon.backends.base import Message, MessageRole

        if payload.backend_name not in daemon._router._instances:
            raise HTTPException(
                status_code=404, detail=f"Backend '{payload.backend_name}' not loaded"
            )

        backend = daemon._router._instances[payload.backend_name]
        try:
            resp = await backend.complete(
                messages=[
                    Message(
                        role=MessageRole.USER,
                        content="Hello! Please reply with exactly 'Hello world'.",
                    )
                ],
                model=payload.model,
                temperature=0.0,
                max_tokens=10,
            )
            return {"status": "success", "response": resp.content}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": str(e)}
