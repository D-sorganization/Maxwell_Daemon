"""Pydantic models for maxwell-daemon.yaml.

Validated at load time so misconfiguration fails fast with a clear error rather
than blowing up mid-run after agents have already spent tokens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class BackendConfig(BaseModel):
    """One LLM backend (Claude, OpenAI, Ollama, ...)."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., description="Backend type: claude, openai, ollama, google, azure")
    model: str = Field(..., description="Default model id for this backend")
    api_key: SecretStr | None = Field(
        None,
        description="API key (supports ${ENV_VAR} substitution). "
        "Wrapped in SecretStr so it won't leak via repr() or model_dump().",
    )
    api_key_secret_ref: str | None = Field(
        None,
        description="OS-backed secret reference for the backend API key.",
    )
    base_url: str | None = None
    enabled: bool = True
    tier_map: dict[str, str] = Field(
        default_factory=dict,
        description="Maps ModelTier names (simple/moderate/complex) → model id. "
        "When set, the router picks by tier; otherwise `model` is used.",
    )
    fallback_backend: str | None = Field(
        None,
        description="Name of a cheaper backend to use when monthly spend exceeds "
        "fallback_threshold_percent of the budget limit.",
    )
    fallback_threshold_percent: float = Field(
        80.0,
        ge=0.0,
        le=100.0,
        description="Switch to fallback_backend when monthly spend reaches this "
        "percentage of the budget limit.",
    )
    cost_per_million_input_tokens: float | None = Field(
        None,
        description="USD per 1M input tokens for cost tracking (e.g. 3.0 for $3/1M). "
        "When set, overrides the built-in pricing table for this backend.",
    )
    cost_per_million_output_tokens: float | None = Field(
        None,
        description="USD per 1M output tokens for cost tracking (e.g. 15.0 for $15/1M). "
        "When set, overrides the built-in pricing table for this backend.",
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
    per_task_limit_usd: float | None = Field(
        None,
        ge=0,
        description="Hard cap on cumulative cost for a single task/agent-loop invocation. "
        "None disables the per-task limit.",
    )


class AgentConfig(BaseModel):
    max_turns: int = Field(200, ge=1)
    discovery_interval_seconds: int = Field(300, ge=10)
    delivery_interval_seconds: int = Field(60, ge=10)
    stall_timeout_seconds: int = Field(
        300,
        ge=0,
        description=(
            "Cancel and retry RUNNING tasks that emit no progress events "
            "for this many seconds. 0 disables stall detection."
        ),
    )
    task_retention_days: int = Field(
        30,
        ge=0,
        description=(
            "Delete terminal tasks and cost records older than this many days. 0 disables pruning."
        ),
    )
    task_prune_interval_seconds: int = Field(
        86_400,
        ge=60,
        description="How often the daemon runs retention pruning while started.",
    )
    live_retention_seconds: int = Field(
        600,
        ge=0,
        description="How long terminal tasks are kept in the hot memory dict before eviction.",
    )
    reasoning_effort: Literal["low", "medium", "high"] = "medium"
    temperature: float = Field(1.0, ge=0.0, le=2.0)
    default_backend: str = "claude"
    concurrency_by_kind: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Optional per-kind concurrency caps applied on top of the "
            "global worker pool. Keys typically use issue modes such as "
            "'plan' or 'implement'."
        ),
    )
    max_queue_depth: int = Field(
        1000,
        ge=1,
        description=(
            "Maximum number of tasks allowed in the queue before "
            "submissions are rejected with 429 Too Many Requests."
        ),
    )
    task_live_retention_seconds: int = Field(
        600,
        ge=0,
        description=(
            "Seconds to keep terminal tasks in memory before evicting them to the database."
        ),
    )


class MemoryConfig(BaseModel):
    """Local memory store and background dream-cycle settings."""

    workspace_path: Path = Field(
        default_factory=lambda: Path.home() / ".local/share/maxwell-daemon",
        description="Workspace root that contains .maxwell/memory and .maxwell/raw_logs.",
    )
    dream_interval_seconds: int = Field(
        0,
        ge=0,
        description="Seconds between background memory anneal cycles. 0 disables cycles.",
    )

    @field_validator("workspace_path", mode="before")
    @classmethod
    def _expand_workspace_path(cls, v: Any) -> Path:
        if isinstance(v, str):
            return Path(v).expanduser()
        if not isinstance(v, Path):
            raise ValueError(f"expected str or Path for 'workspace_path', got {type(v).__name__!r}")
        return v


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
    system_prompt_prefix: str | None = Field(
        None,
        description="Prepended to the default system prompt for this repo's agents.",
    )
    system_prompt_file: Path | None = Field(
        None,
        description="Path to a markdown file whose content is prepended to the system prompt.",
    )

    @field_validator("path", mode="before")
    @classmethod
    def _expand_path(cls, v: Any) -> Path:
        if isinstance(v, str):
            return Path(v).expanduser()
        if not isinstance(v, Path):
            raise ValueError(f"expected str or Path for 'path', got {type(v).__name__!r}")
        return v


class MachineConfig(BaseModel):
    name: str
    host: str = "localhost"
    port: int = 50051
    capacity: int = Field(2, ge=1)
    tags: list[str] = Field(default_factory=list)
    ssh_key: Path | None = None
    tls: bool = Field(
        True, description="Use HTTPS (set False for HTTP-only local/test deployments)"
    )
    tls_verify: bool = Field(
        True, description="Verify TLS certificate (set False for self-signed certs)"
    )


class FleetConfig(BaseModel):
    machines: list[MachineConfig] = Field(default_factory=list)
    discovery_method: Literal["manual", "mdns"] = "manual"
    heartbeat_seconds: int = Field(30, ge=5)
    coordinator_poll_seconds: int = Field(30, ge=5)
    coordinator_url: str | None = Field(
        None,
        description="URL of the coordinator daemon (e.g. https://coordinator:8080)",
    )


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


class DispatchRateLimitConfig(BaseModel):
    """Phase-1 per-endpoint rate limiter for ``POST /api/dispatch``.

    Disabled by default so adding this config block is non-breaking. Operators
    opt in by setting ``enabled: true`` in ``maxwell-daemon.yaml`` under
    ``api.dispatch_rate_limit``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        False,
        description="Enable the sliding-window rate limit on POST /api/dispatch.",
    )
    limit: int = Field(
        10,
        ge=1,
        description="Maximum POST /api/dispatch requests per client per window.",
    )
    window_seconds: int = Field(
        60,
        ge=1,
        description="Sliding-window length in seconds. Counts roll off after this.",
    )


class APIConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(8080, ge=1, le=65535)
    auth_token: str | None = None
    tls_cert: Path | None = None
    tls_key: Path | None = None
    # JWT RBAC — set jwt_secret to enable role-based access control.
    jwt_secret: SecretStr | None = Field(
        None,
        description="HMAC-SHA256 secret for signing JWTs. "
        'Generate with: python -c "import secrets; print(secrets.token_hex(32))". '
        "When set, the daemon issues and validates JWT bearer tokens with role claims.",
    )
    jwt_expiry_seconds: int = Field(
        3600,
        ge=1,
        description="Default JWT lifetime in seconds (default: 1 hour).",
    )
    # Rate limiting. Absent = disabled entirely.
    rate_limit_default: RateLimitConfig | None = None
    rate_limit_groups: dict[str, RateLimitConfig] = Field(default_factory=dict)
    # Phase-1 per-endpoint rate limit for POST /api/dispatch (issue #796).
    # Always present so callers can read .enabled without a None check; the
    # default is disabled so adding this field is a no-op upgrade.
    dispatch_rate_limit: DispatchRateLimitConfig = Field(
        default_factory=DispatchRateLimitConfig,
        description="Per-endpoint rate limit for POST /api/dispatch. Disabled by default.",
    )
    # CORS — list of allowed origins. Empty list = CORS disabled (default for
    # localhost deployments). Set to ["https://your-dashboard.example.com"] in
    # production to restrict cross-origin access (#797).
    cors_allowed_origins: list[str] = Field(
        default_factory=list,
        description=(
            "Allowed CORS origins. Empty list disables CORS middleware. "
            'Use ["*"] for development only — never in production.'
        ),
    )
    # WebSocket connection cap (per-server process). Protects against connection
    # exhaustion attacks (#796). 0 = unlimited (default for localhost).
    websocket_max_connections: int = Field(
        0,
        ge=0,
        description="Maximum concurrent WebSocket connections. 0 = unlimited.",
    )

    def jwt_secret_value(self) -> str | None:
        """Unwrap the JWT secret SecretStr, or None if unset."""
        return self.jwt_secret.get_secret_value() if self.jwt_secret is not None else None

    @model_validator(mode="after")
    def _validate_bind_security(self) -> APIConfig:
        if self.host not in ("127.0.0.1", "localhost", "::1") and self.jwt_secret is None:
            raise ValueError(
                f"Refusing to bind API to {self.host} without JWT configured. "
                "Set api.jwt_secret to expose the daemon on a non-loopback interface."
            )
        return self


class McpServerConfig(BaseModel):
    name: str = Field(..., description="Unique name for the MCP server")
    command: str | None = Field(None, description="Command to execute (e.g. 'npx', 'python')")
    args: list[str] = Field(default_factory=list, description="Arguments for the command")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables")
    transport: Literal["stdio", "sse", "http"] = "stdio"
    url: str | None = Field(None, description="URL for sse or http transport")
    enabled: bool = True


class MaxwellDaemonConfig(BaseModel):
    """Root configuration object."""

    model_config = ConfigDict(extra="forbid")

    version: str = "1"
    role: Literal["standalone", "coordinator", "worker"] = "standalone"
    backends: dict[str, BackendConfig] = Field(default_factory=dict)
    agent: AgentConfig = Field(default_factory=lambda: AgentConfig())
    memory: MemoryConfig = Field(default_factory=lambda: MemoryConfig())
    tools: ToolConfig = Field(default_factory=lambda: ToolConfig())
    repos: list[RepoConfig] = Field(default_factory=list)
    fleet: FleetConfig = Field(default_factory=lambda: FleetConfig())
    api: APIConfig = Field(default_factory=lambda: APIConfig())
    budget: BudgetConfig = Field(default_factory=lambda: BudgetConfig())
    github: GithubConfig = Field(default_factory=lambda: GithubConfig())
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    log_file: Path | None = Field(None, description="Path to write structured rotating logs")

    @field_validator("backends")
    @classmethod
    def _require_default_exists(cls, v: dict[str, BackendConfig]) -> dict[str, BackendConfig]:
        if not v:
            raise ValueError("At least one backend must be configured")
        return v

    @model_validator(mode="after")
    def _validate_backend_references(self) -> MaxwellDaemonConfig:
        known = set(self.backends)

        if self.agent.default_backend not in known:
            raise ValueError(
                f"agent.default_backend '{self.agent.default_backend}' not found in "
                f"backends: {sorted(known)}"
            )

        # Validate per-repo backend overrides
        for repo in self.repos:
            if repo.backend is not None and repo.backend not in known:
                raise ValueError(
                    f"repos['{repo.name}'].backend '{repo.backend}' not found in "
                    f"backends: {sorted(known)}"
                )

        # Validate fallback_backend references within each BackendConfig
        for backend_name, backend_cfg in self.backends.items():
            if (
                backend_cfg.fallback_backend is not None
                and backend_cfg.fallback_backend not in known
            ):
                raise ValueError(
                    f"backends['{backend_name}'].fallback_backend "
                    f"'{backend_cfg.fallback_backend}' not found in backends: {sorted(known)}"
                )

        return self

    def default_backend_config(self) -> BackendConfig:
        name = self.agent.default_backend
        return self.backends[name]

    # ── Config boundary accessors (Law of Demeter) ────────────────────────────
    # Callers should prefer these over traversing sub-objects directly so that
    # internal config layout changes don't ripple through all consumers.

    @property
    def default_backend_name(self) -> str:
        """Shortcut for ``agent.default_backend``."""
        return self.agent.default_backend

    @property
    def api_auth_token(self) -> str | None:
        """Shortcut for ``api.auth_token``."""
        return self.api.auth_token

    @property
    def fleet_coordinator_poll_seconds(self) -> int:
        """Shortcut for ``fleet.coordinator_poll_seconds``."""
        return self.fleet.coordinator_poll_seconds

    @property
    def fleet_heartbeat_seconds(self) -> int:
        """Shortcut for ``fleet.heartbeat_seconds``."""
        return self.fleet.heartbeat_seconds

    @property
    def fleet_machines(self) -> list[MachineConfig]:
        """Shortcut for ``fleet.machines``."""
        return self.fleet.machines

    @property
    def fleet_coordinator_url(self) -> str | None:
        """Shortcut for ``fleet.coordinator_url``."""
        return self.fleet.coordinator_url

    @property
    def memory_workspace_path(self) -> Path:
        """Shortcut for ``memory.workspace_path``."""
        return self.memory.workspace_path

    @property
    def memory_dream_interval_seconds(self) -> int:
        """Shortcut for ``memory.dream_interval_seconds``."""
        return self.memory.dream_interval_seconds

    @property
    def github_routes(self) -> list[WebhookRouteConfig]:
        """Shortcut for ``github.routes``."""
        return self.github.routes

    @property
    def github_allowed_repos(self) -> list[str]:
        """Shortcut for ``github.allowed_repos``."""
        return self.github.allowed_repos

    def github_webhook_secret_value(self) -> str | None:
        """Return the raw webhook secret string, or None if unset."""
        if self.github.webhook_secret is None:
            return None
        return self.github.webhook_secret.get_secret_value()
