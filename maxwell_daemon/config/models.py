"""Pydantic models for maxwell-daemon.yaml.

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
    tier_map: dict[str, str] = Field(
        default_factory=dict,
        description="Maps ModelTier names (simple/moderate/complex) → model id. "
        "When set, the router picks by tier; otherwise `model` is used.",
    )

    def api_key_value(self) -> str | None:
        """Unwrap the SecretStr for passing to adapter constructors."""
        return self.api_key.get_secret_value() if self.api_key is not None else None


class BudgetConfig(BaseModel):
    monthly_limit_usd: float | None = Field(None, ge=0)
    alert_thresholds: list[float] = Field(default_factory=lambda: [0.75, 0.90, 1.0])
    hard_stop: bool = Field(
        False, description="If true, refuse requests that would exceed the budget"
    )
    alert_webhook_url: str | None = Field(
        None, description="POST alerts here when forecast > limit * warn_multiplier"
    )
    alert_warn_multiplier: float = Field(1.1, gt=1.0)
    alert_debounce_hours: int = Field(6, ge=0)


class AgentConfig(BaseModel):
    max_turns: int = Field(200, ge=1)
    discovery_interval_seconds: int = Field(300, ge=10)
    delivery_interval_seconds: int = Field(60, ge=10)
    reasoning_effort: Literal["low", "medium", "high"] = "medium"
    temperature: float = Field(1.0, ge=0.0, le=2.0)
    default_backend: str = "claude"
    task_retention_days: int = Field(
        90,
        ge=1,
        description=(
            "Completed/failed tasks and ledger entries older than this many days "
            "are deleted on the daily prune pass."
        ),
    )


class ToolConfig(BaseModel):
    approval_tier: Literal["suggest", "auto-edit", "full-auto"] = "full-auto"


class RepoConfig(BaseModel):
    name: str
    path: Path
    slots: int = Field(2, ge=1, le=16, description="Max concurrent agents on this repo")
    backend: str | None = Field(None, description="Override default backend for this repo")
    model: str | None = None
    tags: list[str] = Field(default_factory=list)
    # Per-repo overrides of IssueExecutor behaviour. None = use executor default.
    # Lookup lives in maxwell_daemon.core.repo_overrides so the config model stays dumb.
    test_command: list[str] | None = Field(None, min_length=1)
    context_max_chars: int | None = Field(None, ge=0)
    max_test_retries: int | None = Field(None, ge=0)
    max_diff_retries: int | None = Field(None, ge=0)
    # Per-repo system prompt customisation (fixes #151).
    # system_prompt_file takes priority: its contents fully replace the default prompt.
    # system_prompt_prefix is prepended to the default prompt when no file is given.
    system_prompt_prefix: str = Field("", description="Prepended to the default system prompt")
    system_prompt_file: str | None = Field(
        None, description="Path to a file whose contents replace the default system prompt"
    )

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


class WebhookRouteConfig(BaseModel):
    event: str = Field(..., description="GitHub event name: issues, issue_comment, ...")
    action: str = Field(..., description="Event action: opened, closed, created, ...")
    mode: str = Field(default="plan", pattern=r"^(plan|implement)$")
    label: str | None = Field(None, description="Required issue label, if any")
    trigger: str | None = Field(None, description="Required comment substring, if any")


class GithubConfig(BaseModel):
    webhook_secret: SecretStr | None = None
    allowed_repos: list[str] = Field(default_factory=list)
    routes: list[WebhookRouteConfig] = Field(default_factory=list)


class RateLimitConfig(BaseModel):
    rate: float = Field(10.0, gt=0, description="Tokens refilled per second")
    burst: int = Field(50, ge=1, description="Bucket capacity (max burst)")


class APIConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(8080, ge=1, le=65535)
    auth_token: str | None = None
    tls_cert: Path | None = None
    tls_key: Path | None = None
    # Rate limiting. Absent = disabled entirely.
    rate_limit_default: RateLimitConfig | None = None
    rate_limit_groups: dict[str, RateLimitConfig] = Field(default_factory=dict)


class MaxwellDaemonConfig(BaseModel):
    """Root configuration object."""

    model_config = ConfigDict(extra="forbid")

    version: str = "1"
    backends: dict[str, BackendConfig] = Field(default_factory=dict)
    agent: AgentConfig = Field(default_factory=lambda: AgentConfig())
    tools: ToolConfig = Field(default_factory=lambda: ToolConfig())
    repos: list[RepoConfig] = Field(default_factory=list)
    fleet: FleetConfig = Field(default_factory=lambda: FleetConfig())
    api: APIConfig = Field(default_factory=lambda: APIConfig())
    budget: BudgetConfig = Field(default_factory=lambda: BudgetConfig())
    github: GithubConfig = Field(default_factory=lambda: GithubConfig())

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
