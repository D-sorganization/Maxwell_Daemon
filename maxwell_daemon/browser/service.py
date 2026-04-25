"""Typed browser automation contracts.

This module intentionally does not import Playwright. It defines the stable
service and runner contracts while keeping default installations and CI free of
browser runtime requirements.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from maxwell_daemon.contracts import require
from maxwell_daemon.core.artifacts import Artifact, ArtifactKind


class BrowserAction(str, Enum):
    """Supported browser action contracts."""

    SNAPSHOT = "snapshot"
    SCREENSHOT = "screenshot"


class BrowserUnavailableError(RuntimeError):
    """Raised when browser automation was requested but no runner is configured."""


class BrowserArtifactError(RuntimeError):
    """Raised when browser output cannot be converted into durable artifacts."""


class ConsoleLogEntry(BaseModel):
    """One browser console log captured during a browser action."""

    level: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
        if self.allowed_hosts and not any(
            _host_matches(host, allowed) for allowed in self.allowed_hosts
        ):
            raise ValueError(f"browser url host {host!r} is not in allowed_hosts")
        return self


class BrowserResult(BaseModel):
    """Normalized browser automation result."""

    url: str = Field(..., min_length=1)
    action: BrowserAction
    title: str | None = None
    text: str = ""
    console_logs: tuple[ConsoleLogEntry, ...] = ()
    page_errors: tuple[str, ...] = ()
    screenshot_png: bytes | None = Field(default=None, exclude=True, repr=False)
    screenshot_artifact_id: str | None = None
    console_artifact_id: str | None = None
    page_error_artifact_id: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class BrowserRunner(Protocol):
    """Async runner contract implemented by concrete browser backends."""

    async def run(self, request: BrowserRequest) -> BrowserResult: ...


class BrowserArtifactSink(Protocol):
    """Minimal durable artifact sink used by browser automation."""

    def put_bytes(
        self,
        *,
        kind: ArtifactKind,
        name: str,
        data: bytes,
        task_id: str | None = None,
        work_item_id: str | None = None,
        media_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> Artifact: ...

    def put_text(
        self,
        *,
        kind: ArtifactKind,
        name: str,
        text: str,
        task_id: str | None = None,
        work_item_id: str | None = None,
        media_type: str = "text/plain; charset=utf-8",
        metadata: dict[str, Any] | None = None,
    ) -> Artifact: ...


class UnavailableBrowserRunner:
    """Default runner used until an optional browser backend is installed."""

    async def run(self, request: BrowserRequest) -> BrowserResult:
        raise BrowserUnavailableError(
            "browser automation requires a configured BrowserRunner "
            "(for example, a Playwright-backed runner)"
        )


class BrowserService:
    """Small orchestration wrapper around an injected browser runner."""

    def __init__(
        self,
        runner: BrowserRunner | None = None,
        *,
        artifact_store: BrowserArtifactSink | None = None,
        task_id: str | None = None,
    ) -> None:
        if artifact_store is not None and not task_id:
            raise ValueError(
                "BrowserService requires task_id when artifact_store is configured"
            )
        self._runner = runner or UnavailableBrowserRunner()
        self._artifact_store = artifact_store
        self._task_id = task_id

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
        return self._store_artifacts(request, result)

    def _store_artifacts(
        self, request: BrowserRequest, result: BrowserResult
    ) -> BrowserResult:
        store = self._artifact_store
        if store is None:
            if result.screenshot_png is not None:
                raise BrowserArtifactError(
                    "browser runner returned screenshot bytes but no artifact_store is configured"
                )
            return result

        task_id = self._task_id
        if task_id is None:
            raise BrowserArtifactError("browser artifact storage requires a task_id")

        updates: dict[str, object] = {}
        action_value = (
            request.action.value
            if isinstance(request.action, BrowserAction)
            else request.action
        )
        metadata = {
            "url": request.url,
            "action": action_value,
            "title": result.title,
        }
        if result.screenshot_png is not None:
            artifact = store.put_bytes(
                task_id=task_id,
                kind=ArtifactKind.SCREENSHOT,
                name="browser-screenshot.png",
                data=result.screenshot_png,
                media_type="image/png",
                metadata=metadata,
            )
            updates["screenshot_artifact_id"] = artifact.id
            updates["screenshot_png"] = None

        if result.console_logs:
            payload = [entry.model_dump(mode="json") for entry in result.console_logs]
            artifact = store.put_text(
                task_id=task_id,
                kind=ArtifactKind.BROWSER_CONSOLE,
                name="browser-console.json",
                text=json.dumps(payload, indent=2, sort_keys=True) + "\n",
                media_type="application/json",
                metadata=metadata,
            )
            updates["console_artifact_id"] = artifact.id

        if result.page_errors:
            artifact = store.put_text(
                task_id=task_id,
                kind=ArtifactKind.PAGE_ERROR,
                name="browser-page-errors.json",
                text=json.dumps(list(result.page_errors), indent=2, sort_keys=True)
                + "\n",
                media_type="application/json",
                metadata=metadata,
            )
            updates["page_error_artifact_id"] = artifact.id

        if not updates:
            return result
        return result.model_copy(update=updates)


def _host_matches(host: str, allowed: str) -> bool:
    if allowed.startswith("*."):
        suffix = allowed[2:]
        return host == suffix or host.endswith(f".{suffix}")
    return host == allowed
