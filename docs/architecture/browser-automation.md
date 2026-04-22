# Browser Automation

Maxwell's browser automation layer gives agents a governed way to inspect web
apps without putting Playwright in the base installation.

## Installation

Install the optional browser extra when a worker should run browser tasks:

```bash
pip install "maxwell-daemon[browser]"
python -m playwright install chromium
```

Workers that do not install the extra can still import Maxwell. Browser tools
raise a typed unavailable error instead of failing at import time.

## Security Model

Browser requests are validated before navigation:

- Only `http` and `https` URLs are accepted.
- URLs with embedded credentials are rejected.
- Callers can provide an `allowed_hosts` allowlist, including `*.example.com`
  wildcards.
- Browser tools are registered only when a `BrowserService` is explicitly
  supplied to the tool registry.

The service boundary is intentional. Agent tools call `BrowserService`; they do
not manipulate Playwright objects directly.

## Artifact Contract

Browser output is converted into durable task artifacts:

- Screenshots are stored as `screenshot` artifacts with media type `image/png`.
- Console logs are stored as `browser_console` JSON artifacts.
- Page errors are stored as `page_error` JSON artifacts.

`BrowserService` requires a `task_id` when an artifact store is configured. If a
runner returns raw screenshot bytes without an artifact store, the service
raises `BrowserArtifactError` rather than exposing image bytes through a text
tool result.

## Built-In Tools

When browser support is enabled, the default tool registry can expose:

```text
open_browser_url(url, allowed_hosts=None, timeout_seconds=None)
browser_screenshot(url, allowed_hosts=None, timeout_seconds=None)
```

`open_browser_url` returns a text snapshot. `browser_screenshot` captures a
screenshot and returns the durable artifact id along with any console or page
error artifacts produced during the run.
