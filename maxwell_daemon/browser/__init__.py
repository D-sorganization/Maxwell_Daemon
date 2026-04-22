"""Browser automation foundation for multimodal agent tasks."""

from maxwell_daemon.browser.service import (
    BrowserAction,
    BrowserRequest,
    BrowserResult,
    BrowserRunner,
    BrowserService,
    BrowserUnavailableError,
    UnavailableBrowserRunner,
)

__all__ = [
    "BrowserAction",
    "BrowserRequest",
    "BrowserResult",
    "BrowserRunner",
    "BrowserService",
    "BrowserUnavailableError",
    "UnavailableBrowserRunner",
]
