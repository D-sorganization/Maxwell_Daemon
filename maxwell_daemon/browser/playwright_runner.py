"""Optional Playwright-backed browser runner."""

from __future__ import annotations

from maxwell_daemon.browser.service import (
    BrowserAction,
    BrowserRequest,
    BrowserResult,
    BrowserUnavailableError,
    ConsoleLogEntry,
)

_BROWSER_NAMES = frozenset(("chromium", "firefox", "webkit"))


class PlaywrightBrowserRunner:
    """Browser runner that captures text, console logs, page errors, and screenshots."""

    def __init__(
        self,
        *,
        browser_name: str = "chromium",
        headless: bool = True,
        full_page_screenshots: bool = True,
    ) -> None:
        if browser_name not in _BROWSER_NAMES:
            raise ValueError("browser_name must be one of: chromium, firefox, webkit")
        self._browser_name = browser_name
        self._headless = headless
        self._full_page_screenshots = full_page_screenshots

    async def run(self, request: BrowserRequest) -> BrowserResult:
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise BrowserUnavailableError(
                "Playwright browser automation is not installed; install "
                "`maxwell-daemon[browser]` and run `playwright install chromium`"
            ) from exc

        timeout_ms = request.timeout_seconds * 1000
        console_logs: list[ConsoleLogEntry] = []
        page_errors: list[str] = []

        try:
            async with async_playwright() as playwright:
                launcher = getattr(playwright, self._browser_name)
                browser = await launcher.launch(headless=self._headless)
                try:
                    page = await browser.new_page()
                    page.on(
                        "console",
                        lambda message: console_logs.append(
                            ConsoleLogEntry(level=message.type, message=message.text)
                        ),
                    )
                    page.on("pageerror", lambda error: page_errors.append(str(error)))

                    await page.goto(
                        request.url,
                        wait_until="networkidle",
                        timeout=timeout_ms,
                    )
                    title = await page.title()
                    text = await page.locator("body").inner_text(timeout=min(timeout_ms, 5000))
                    screenshot_png = None
                    if request.action == BrowserAction.SCREENSHOT:
                        screenshot_png = await page.screenshot(
                            full_page=self._full_page_screenshots
                        )
                    return BrowserResult(
                        url=request.url,
                        action=request.action,
                        title=title,
                        text=text,
                        console_logs=tuple(console_logs),
                        page_errors=tuple(page_errors),
                        screenshot_png=screenshot_png,
                    )
                finally:
                    await browser.close()
        except PlaywrightError as exc:  # pragma: no cover - needs browser binaries
            message = str(exc)
            if "Executable doesn't exist" in message or "playwright install" in message:
                raise BrowserUnavailableError(
                    "Playwright browser binaries are not installed; run "
                    "`playwright install chromium`"
                ) from exc
            raise
