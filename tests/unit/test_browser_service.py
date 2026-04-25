from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from maxwell_daemon.browser import (
    BrowserAction,
    BrowserArtifactError,
    BrowserRequest,
    BrowserResult,
    BrowserService,
    BrowserUnavailableError,
    ConsoleLogEntry,
    PlaywrightBrowserRunner,
)
from maxwell_daemon.core.artifacts import ArtifactKind, ArtifactStore
from maxwell_daemon.tools.builtins import build_default_registry


class FakeBrowserRunner:
    def __init__(
        self,
        *,
        screenshot_png: bytes | None = None,
        console_logs: tuple[ConsoleLogEntry, ...] = (),
        page_errors: tuple[str, ...] = (),
    ) -> None:
        self.requests: list[BrowserRequest] = []
        self.screenshot_png = screenshot_png
        self.console_logs = console_logs
        self.page_errors = page_errors

    async def run(self, request: BrowserRequest) -> BrowserResult:
        self.requests.append(request)
        return BrowserResult(
            url=request.url,
            action=request.action,
            title="Example",
            text="Example page text",
            screenshot_png=self.screenshot_png,
            console_logs=self.console_logs,
            page_errors=self.page_errors,
        )


def test_browser_request_rejects_non_http_urls() -> None:
    with pytest.raises(ValidationError, match="http or https"):
        BrowserRequest(url="file:///etc/passwd")


def test_browser_request_rejects_credentials_in_url() -> None:
    with pytest.raises(ValidationError, match="credentials"):
        BrowserRequest(url="https://user:pass@example.com")


def test_browser_request_enforces_allowed_hosts() -> None:
    with pytest.raises(ValidationError, match="not in allowed_hosts"):
        BrowserRequest(url="https://evil.example", allowed_hosts=("example.com",))


def test_browser_request_accepts_wildcard_allowed_hosts() -> None:
    request = BrowserRequest(
        url="https://docs.example.com/path", allowed_hosts=("*.example.com",)
    )

    assert request.url == "https://docs.example.com/path"


def test_playwright_runner_rejects_unknown_browser() -> None:
    with pytest.raises(ValueError, match="browser_name"):
        PlaywrightBrowserRunner(browser_name="chrome")


async def test_browser_service_uses_injected_runner() -> None:
    runner = FakeBrowserRunner()
    service = BrowserService(runner)
    request = BrowserRequest(url="https://example.com", action=BrowserAction.SNAPSHOT)

    result = await service.run(request)

    assert runner.requests == [request]
    assert result.title == "Example"
    assert result.text == "Example page text"


async def test_default_browser_service_reports_unavailable() -> None:
    service = BrowserService()
    request = BrowserRequest(url="https://example.com")

    with pytest.raises(BrowserUnavailableError, match="configured BrowserRunner"):
        await service.run(request)


def test_browser_service_requires_task_for_artifact_storage(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts.db", blob_root=tmp_path / "blobs")

    with pytest.raises(ValueError, match="task_id"):
        BrowserService(FakeBrowserRunner(), artifact_store=store)


async def test_browser_service_refuses_raw_screenshot_without_artifact_store() -> None:
    service = BrowserService(FakeBrowserRunner(screenshot_png=b"\x89PNG\r\n"))
    request = BrowserRequest(url="https://example.com", action=BrowserAction.SCREENSHOT)

    with pytest.raises(BrowserArtifactError, match="artifact_store"):
        await service.run(request)


async def test_browser_service_stores_screenshot_console_and_page_error_artifacts(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts.db", blob_root=tmp_path / "blobs")
    service = BrowserService(
        FakeBrowserRunner(
            screenshot_png=b"\x89PNG\r\n",
            console_logs=(ConsoleLogEntry(level="log", message="ready"),),
            page_errors=("boom",),
        ),
        artifact_store=store,
        task_id="task-browser",
    )
    request = BrowserRequest(url="https://example.com", action=BrowserAction.SCREENSHOT)

    result = await service.run(request)

    assert result.screenshot_png is None
    assert result.screenshot_artifact_id is not None
    assert result.console_artifact_id is not None
    assert result.page_error_artifact_id is not None

    screenshot = store.get(result.screenshot_artifact_id)
    assert screenshot is not None
    assert screenshot.kind is ArtifactKind.SCREENSHOT
    assert screenshot.media_type == "image/png"
    assert screenshot.task_id == "task-browser"
    assert screenshot.metadata["url"] == "https://example.com"
    assert store.read_bytes(screenshot.id) == b"\x89PNG\r\n"

    console = store.get(result.console_artifact_id)
    assert console is not None
    assert console.kind is ArtifactKind.BROWSER_CONSOLE
    assert '"message": "ready"' in store.read_text(console.id)

    page_error = store.get(result.page_error_artifact_id)
    assert page_error is not None
    assert page_error.kind is ArtifactKind.PAGE_ERROR
    assert '"boom"' in store.read_text(page_error.id)


async def test_browser_tool_is_optional_and_uses_service(tmp_path: Path) -> None:
    default_registry = build_default_registry(tmp_path)
    assert "open_browser_url" not in default_registry.names()
    assert "browser_screenshot" not in default_registry.names()

    runner = FakeBrowserRunner()
    registry = build_default_registry(tmp_path, browser_service=BrowserService(runner))

    result = await registry.invoke(
        "open_browser_url",
        {
            "url": "https://example.com",
            "allowed_hosts": ["example.com"],
        },
    )

    assert result.is_error is False
    assert "Example page text" in result.content
    assert runner.requests[0].url == "https://example.com"


async def test_browser_screenshot_tool_returns_artifact_ids(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts.db", blob_root=tmp_path / "blobs")
    runner = FakeBrowserRunner(
        screenshot_png=b"\x89PNG\r\n",
        console_logs=(ConsoleLogEntry(level="warning", message="slow paint"),),
    )
    registry = build_default_registry(
        tmp_path,
        browser_service=BrowserService(
            runner,
            artifact_store=store,
            task_id="task-browser",
        ),
    )

    result = await registry.invoke(
        "browser_screenshot",
        {
            "url": "https://example.com",
            "allowed_hosts": ["example.com"],
        },
    )

    assert result.is_error is False
    assert "screenshot_artifact_id:" in result.content
    assert "console_artifact_id:" in result.content
    assert runner.requests[0].action == BrowserAction.SCREENSHOT
