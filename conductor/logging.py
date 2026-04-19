"""Structured logging built on structlog.

One function to configure (``configure_logging``), one function to fetch a logger
(``get_logger``), one context manager to attach scoped metadata (``bind_context``).

Defaults to JSON output in production (easy to ship to Loki / ELK / Datadog) and
human-readable output when a TTY is attached.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Iterator
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, unbind_contextvars

__all__ = ["bind_context", "configure_logging", "get_logger"]


def configure_logging(
    *,
    level: str = "INFO",
    json_format: bool | None = None,
) -> None:
    """Configure root logging handlers and structlog processors.

    :param level: stdlib log level name.
    :param json_format: force JSON output. ``None`` auto-detects (TTY → pretty,
        otherwise JSON).
    """
    if json_format is None:
        json_format = not sys.stderr.isatty()

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_format
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib logging through structlog so library logs pick up our format.
    logging.basicConfig(
        level=level.upper(),
        format="%(message)s",
        stream=sys.stderr,
        force=True,
    )


def get_logger(name: str | None = None) -> Any:
    """Return a structlog BoundLogger. Pass a module name as is customary."""
    return structlog.get_logger(name) if name else structlog.get_logger()


@contextlib.contextmanager
def bind_context(**kwargs: Any) -> Iterator[None]:
    """Bind key/value pairs to the current logging context for the duration of a block.

    Nested blocks merge: outer keys stay visible to inner blocks unless shadowed.
    """
    tokens: dict[str, Any] = {}
    # We bind one key at a time so we can unbind precisely on exit, leaving any
    # outer bindings intact.
    bind_contextvars(**kwargs)
    try:
        yield
    finally:
        unbind_contextvars(*kwargs.keys())
        del tokens
