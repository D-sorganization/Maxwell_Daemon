"""Per-backend concurrent request limiting and smart retry for 429 responses.

Two primitives are exported:

``BackendConcurrencyLimiter``
    A semaphore-based limiter keyed by backend name. Each backend gets its own
    :class:`asyncio.Semaphore` so a saturated backend cannot starve other
    backends. Obtain a slot with ``async with limiter.acquire(backend_name)``.

``retry_on_rate_limit``
    An async function decorator that retries calls that raise HTTP 429 / quota
    errors with exponential back-off and jitter. Integrates with the standard
    Python exception hierarchy used by httpx, httpcore, and the Anthropic /
    OpenAI SDKs. Can also be used as a stand-alone async context manager
    ``async with retry_on_rate_limit()``.

``with_concurrency_limit``
    A thin decorator that acquires a slot from a given limiter before calling
    the wrapped coroutine and releases it when the call finishes (normal or
    exceptional).

Usage example::

    # At module level for a backend:
    from maxwell_daemon.backends.concurrency import (
        BackendConcurrencyLimiter,
        retry_on_rate_limit,
        with_concurrency_limit,
    )

    _limiter = BackendConcurrencyLimiter.get_global()

    class MyBackend(ILLMBackend):
        name = "my-backend"

        @with_concurrency_limit(_limiter, "my-backend")
        @retry_on_rate_limit()
        async def complete(self, messages, *, model, ...):
            ...
"""

from __future__ import annotations

import asyncio
import functools
import math
import random
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from maxwell_daemon.logging import get_logger

__all__ = [
    "BackendConcurrencyLimiter",
    "retry_on_rate_limit",
    "with_concurrency_limit",
]

log = get_logger(__name__)

_F = TypeVar("_F", bound=Callable[..., Coroutine[Any, Any, Any]])

# ---------------------------------------------------------------------------
# Exceptions that indicate a rate-limit / quota response
# ---------------------------------------------------------------------------

#: Exception class names (case-insensitive substrings) that signal a 429.
_RATE_LIMIT_SIGNALS = frozenset(
    {
        "ratelimiterror",
        "ratelimit",
        "toomanyrequests",
        "quota",
        "throttle",
    }
)


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Return True for HTTP 429 / quota errors from any SDK."""
    cls_name = type(exc).__name__.lower()
    if any(s in cls_name for s in _RATE_LIMIT_SIGNALS):
        return True
    # Check for a .status_code / .response.status_code attribute (httpx, requests …)
    code = getattr(exc, "status_code", None)
    if code is None:
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None)
    return code == 429


# ---------------------------------------------------------------------------
# retry_on_rate_limit
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _retry_ctx(
    *,
    max_attempts: int,
    base_delay: float,
    max_delay: float,
    jitter: bool,
) -> AsyncIterator[None]:
    """Async context manager that retries the body on rate-limit errors."""
    attempt = 0
    while True:
        try:
            yield
            return
        except Exception as exc:
            attempt += 1
            if not _is_rate_limit_error(exc) or attempt >= max_attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            if jitter:
                delay = delay * (0.5 + random.random() * 0.5)
            log.warning(
                "backend_rate_limited",
                attempt=attempt,
                max_attempts=max_attempts,
                retry_after_seconds=round(delay, 2),
                exc=str(exc),
            )
            await asyncio.sleep(delay)


def retry_on_rate_limit(
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
) -> Callable[[_F], _F]:
    """Decorator: retry the wrapped coroutine on HTTP 429 / rate-limit errors.

    Back-off strategy: exponential with optional ±50 % jitter.

    :param max_attempts: Total attempts (not retries). ``1`` means no retries.
    :param base_delay: Initial delay in seconds before the first retry.
    :param max_delay: Cap on the back-off delay.
    :param jitter: Add ±50 % random jitter to avoid thundering-herd.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if base_delay < 0:
        raise ValueError("base_delay must be >= 0")

    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            while True:
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    attempt += 1
                    if not _is_rate_limit_error(exc) or attempt >= max_attempts:
                        raise
                    delay = min(base_delay * math.pow(2, attempt - 1), max_delay)
                    if jitter:
                        delay = delay * (0.5 + random.random() * 0.5)
                    log.warning(
                        "backend_rate_limited",
                        fn=fn.__qualname__,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        retry_after_seconds=round(delay, 2),
                        exc=str(exc),
                    )
                    await asyncio.sleep(delay)

        return wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# BackendConcurrencyLimiter
# ---------------------------------------------------------------------------


class BackendConcurrencyLimiter:
    """Semaphore pool — one semaphore per backend name.

    The default concurrency limit is 10 concurrent requests per backend.
    Override per-backend with :meth:`set_limit`.

    A process-wide singleton is available via :meth:`get_global`::

        limiter = BackendConcurrencyLimiter.get_global()
        async with limiter.acquire("claude"):
            response = await client.complete(...)
    """

    DEFAULT_LIMIT = 10

    _global: BackendConcurrencyLimiter | None = None

    def __init__(self, default_limit: int = DEFAULT_LIMIT) -> None:
        if default_limit < 1:
            raise ValueError("default_limit must be >= 1")
        self._default_limit = default_limit
        self._limits: dict[str, int] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_limit(self, backend_name: str, limit: int) -> None:
        """Override the concurrency limit for ``backend_name``.

        Must be called before the first :meth:`acquire` for this backend,
        otherwise the change is ignored for any already-created semaphore.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        self._limits[backend_name] = limit
        # Drop any cached semaphore so the new limit takes effect.
        self._semaphores.pop(backend_name, None)

    # ------------------------------------------------------------------
    # Semaphore access
    # ------------------------------------------------------------------

    def _get_semaphore(self, backend_name: str) -> asyncio.Semaphore:
        if backend_name not in self._semaphores:
            limit = self._limits.get(backend_name, self._default_limit)
            self._semaphores[backend_name] = asyncio.Semaphore(limit)
        return self._semaphores[backend_name]

    @asynccontextmanager
    async def acquire(self, backend_name: str) -> AsyncIterator[None]:
        """Async context manager that blocks until a slot is available.

        Usage::

            async with limiter.acquire("claude"):
                result = await backend.complete(...)
        """
        sem = self._get_semaphore(backend_name)
        async with sem:
            log.debug("backend_slot_acquired", backend=backend_name)
            try:
                yield
            finally:
                log.debug("backend_slot_released", backend=backend_name)

    # ------------------------------------------------------------------
    # Global singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_global(cls) -> BackendConcurrencyLimiter:
        """Return (and lazily create) the process-wide limiter instance."""
        if cls._global is None:
            cls._global = cls()
        return cls._global

    @classmethod
    def reset_global(cls) -> None:
        """Reset the global instance. Useful in tests."""
        cls._global = None


# ---------------------------------------------------------------------------
# with_concurrency_limit decorator
# ---------------------------------------------------------------------------


def with_concurrency_limit(
    limiter: BackendConcurrencyLimiter,
    backend_name: str,
) -> Callable[[_F], _F]:
    """Decorator: acquire a slot from ``limiter`` before calling the wrapped coroutine.

    Pairs well with :func:`retry_on_rate_limit`::

        @with_concurrency_limit(_limiter, "my-backend")
        @retry_on_rate_limit()
        async def complete(self, ...):
            ...
    """

    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            async with limiter.acquire(backend_name):
                return await fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
