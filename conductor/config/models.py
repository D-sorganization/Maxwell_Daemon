"""Pydantic models for conductor.yaml.

Validated at load time so misconfiguration fails fast with a clear error rather
than blowing up mid-run after agents have already spent tokens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class BackendConfig(BaseModel):
    """One LLM backend (Claude, OpenAI, Ollama, ...)."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(..., description="Backend type: claude, openai, ollama, google, azure")
    model: str = Field(..., description="Default model id for this backend")
    api_key: SecretStr | None = Field(
        None,
        description="API key (supports ${ENV_VAR} substitution). "
        "Wrapped in SecretStr so it won't leak via repr() or model_dump().",
    )
    base_url: str | None = None
    enabled: bool = True

    def api_key_value(self) -> str | None:
        """Unwrap the SecretStr for passing to adapter constructors."""
        return self.api_key.get_secret_value() if self.api_key is not None else None


class BudgetConfig(BaseModel):
    monthly_limit_usd: float | None = Field(None, ge=0)
    alert_thresholds: list[float] = Field(default_factory=lambda: [0.75, 0.90, 1.0])
    hard_stop: bool = Field(
        False, description="If true, refuse requests that would exceed the budget"
    )


class AgentConfig(BaseModel):
    max_turns: int = Field(200, ge=1)
    discovery_interval_seconds: int = Field(300, ge=10)
    delivery_interval_seconds: int = Field(60, ge=10)
    reasoning_effort: Literal["low", "medium", "high"] = "medium"
    temperature: float = Field(1.0, ge=0.0, le=2.0)
    default_backend: str = "claude"


class RepoConfig(BaseModel):
    name: str
    path: Path
    slots: int = Field(2, ge=1, le=16, description="Max concurrent agents on this repo")
    backend: str | None = Field(None, description="Override default backend for this repo")
    model: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("path", mode="before")
    @classmethod
    def _expand_path(cls, v: Any) -> Path:
        if isinstance(v, str):
            return Path(v).expanduser()
        assert isinstance(v, Path)
        return v


class MachineConfig(BaseModel):
    name: str
    host: str = "localhost"
    port: int = 50051
    capacity: int = Field(2, ge=1)
    tags: list[str] = Field(default_factory=list)
    ssh_key: Path | None = None


class FleetConfig(BaseModel):
    machines: list[MachineConfig] = Field(default_factory=list)
    discovery_method: Literal["manual", "mdns"] = "manual"
    heartbeat_seconds: int = Field(30, ge=5)


class APIConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(8080, ge=1, le=65535)
    auth_token: str | None = None
    tls_cert: Path | None = None
    tls_key: Path | None = None


class ConductorConfig(BaseModel):
    """Root configuration object."""

    model_config = ConfigDict(extra="forbid")

    version: str = "1"
    backends: dict[str, BackendConfig] = Field(default_factory=dict)
    agent: AgentConfig = Field(default_factory=lambda: AgentConfig())
    repos: list[RepoConfig] = Field(default_factory=list)
    fleet: FleetConfig = Field(default_factory=lambda: FleetConfig())
    api: APIConfig = Field(default_factory=lambda: APIConfig())
    budget: BudgetConfig = Field(default_factory=lambda: BudgetConfig())

    @field_validator("backends")
    @classmethod
    def _require_default_exists(cls, v: dict[str, BackendConfig]) -> dict[str, BackendConfig]:
        if not v:
            raise ValueError("At least one backend must be configured")
        return v

    def default_backend_config(self) -> BackendConfig:
        name = self.agent.default_backend
        if name not in self.backends:
            raise ValueError(
                f"default_backend '{name}' not found in backends: {sorted(self.backends)}"
            )
        return self.backends[name]
