from __future__ import annotations

import pytest
from pydantic import ValidationError

from maxwell_daemon.browser import (
    BrowserAction,
    BrowserRequest,
    BrowserResult,
    BrowserService,
    BrowserUnavailableError,
)
from maxwell_daemon.tools.builtins import build_default_registry


class FakeBrowserRunner:
    def __init__(self) -> None:
        self.requests: list[BrowserRequest] = []

    async def run(self, request: BrowserRequest) -> BrowserResult:
        self.requests.append(request)
        return BrowserResult(
            url=request.url,
            action=request.action,
            title="Example",
            text="Example page text",
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
    request = BrowserRequest(url="https://docs.example.com/path", allowed_hosts=("*.example.com",))

    assert request.url == "https://docs.example.com/path"


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


async def test_browser_tool_is_optional_and_uses_service(tmp_path) -> None:
    default_registry = build_default_registry(tmp_path)
    assert "open_browser_url" not in default_registry.names()

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
