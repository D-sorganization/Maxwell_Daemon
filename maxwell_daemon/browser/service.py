"""Typed browser automation contracts.

This module intentionally does not import Playwright. It defines the stable
surface a Playwright-backed runner can implement later while keeping default
installations and CI free of browser runtime requirements.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from maxwell_daemon.contracts import require


class BrowserAction(str, Enum):
    """Supported browser action contracts."""

    SNAPSHOT = "snapshot"
    SCREENSHOT = "screenshot"


class BrowserUnavailableError(RuntimeError):
    """Raised when browser automation was requested but no runner is configured."""


class BrowserRequest(BaseModel):
    """Validated browser automation request.

    ``allowed_hosts`` is a defense-in-depth allowlist. Empty means no host
    allowlist was supplied by the caller; non-empty values must match exactly,
    or as ``*.example.com`` suffix wildcards.
    """

    model_config = ConfigDict(use_enum_values=True)

    url: str = Field(..., min_length=1)
    action: BrowserAction = BrowserAction.SNAPSHOT
    allowed_hosts: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("browser url must use http or https")
        if not parsed.hostname:
            raise ValueError("browser url must include a hostname")
        if parsed.username or parsed.password:
            raise ValueError("browser url must not include credentials")
        return value

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _normalize_allowed_hosts(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = (value,)
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("allowed_hosts must be a string or sequence of strings")
        hosts = tuple(str(item).strip().lower() for item in value)
        if any(not host for host in hosts):
            raise ValueError("allowed_hosts entries must be non-empty")
        return hosts

    @model_validator(mode="after")
    def _host_is_allowed(self) -> BrowserRequest:
        parsed = urlparse(self.url)
        host = (parsed.hostname or "").lower()
        if self.allowed_hosts and not any(_host_matches(host, allowed) for allowed in self.allowed_hosts):
            raise ValueError(f"browser url host {host!r} is not in allowed_hosts")
        return self


class BrowserResult(BaseModel):
    """Normalized browser automation result."""

    url: str = Field(..., min_length=1)
    action: BrowserAction
    title: str | None = None
    text: str = ""
    screenshot_artifact_id: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class BrowserRunner(Protocol):
    """Async runner contract implemented by concrete browser backends."""

    async def run(self, request: BrowserRequest) -> BrowserResult: ...


class UnavailableBrowserRunner:
    """Default runner used until an optional browser backend is installed."""

    async def run(self, request: BrowserRequest) -> BrowserResult:
        raise BrowserUnavailableError(
            "browser automation requires a configured BrowserRunner "
            "(for example, a Playwright-backed runner)"
        )


class BrowserService:
    """Small orchestration wrapper around an injected browser runner."""

    def __init__(self, runner: BrowserRunner | None = None) -> None:
        self._runner = runner or UnavailableBrowserRunner()

    async def run(self, request: BrowserRequest) -> BrowserResult:
        require(
            isinstance(request, BrowserRequest),
            "BrowserService.run: request must be a BrowserRequest",
        )
        result = await self._runner.run(request)
        if result.url != request.url:
            raise ValueError("browser runner returned a result for a different url")
        if result.action != request.action:
            raise ValueError("browser runner returned a result for a different action")
        return result


def _host_matches(host: str, allowed: str) -> bool:
    if allowed.startswith("*."):
        suffix = allowed[2:]
        return host == suffix or host.endswith(f".{suffix}")
    return host == allowed
