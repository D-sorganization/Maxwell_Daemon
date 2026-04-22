"""Browser automation foundation for multimodal agent tasks."""

from maxwell_daemon.browser.playwright_runner import PlaywrightBrowserRunner
from maxwell_daemon.browser.service import (
    BrowserAction,
    BrowserArtifactError,
    BrowserArtifactSink,
    BrowserRequest,
    BrowserResult,
    BrowserRunner,
    BrowserService,
    BrowserUnavailableError,
    ConsoleLogEntry,
    UnavailableBrowserRunner,
)

__all__ = [
    "BrowserAction",
    "BrowserArtifactError",
    "BrowserArtifactSink",
    "BrowserRequest",
    "BrowserResult",
    "BrowserRunner",
    "BrowserService",
    "BrowserUnavailableError",
    "ConsoleLogEntry",
    "PlaywrightBrowserRunner",
    "UnavailableBrowserRunner",
]
