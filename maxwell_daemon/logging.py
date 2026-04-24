"""Structured logging built on structlog.

One function to configure (``configure_logging``), one function to fetch a logger
(``get_logger``), one context manager to attach scoped metadata (``bind_context``).

Defaults to JSON output in production (easy to ship to Loki / ELK / Datadog) and
human-readable output when a TTY is attached.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, unbind_contextvars

__all__ = ["bind_context", "configure_logging", "get_logger"]

_REDACT_KEYS = {"api_key", "password", "token", "secret", "authorization"}


def _redact_value(val: Any) -> Any:
    if not isinstance(val, str):
        return "***"
    if len(val) <= 12:
        return "***"
    return f"{val[:8]}...{val[-4:]}"


def _redact_secrets_processor(
    logger: structlog.types.WrappedLogger, name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    if os.environ.get("MAXWELL_REDACT_LOGS", "1") != "1":
        return event_dict

    for key, value in event_dict.items():
        if any(redact_key in key.lower() for redact_key in _REDACT_KEYS):
            event_dict[key] = _redact_value(value)
    return event_dict


def configure_logging(
    *,
    level: str = "INFO",
    json_format: bool | None = None,
    log_file: str | Path | None = None,
) -> None:
    """Configure root logging handlers and structlog processors.

    :param level: stdlib log level name.
    :param json_format: force JSON output. ``None`` auto-detects (TTY → pretty,
        otherwise JSON).
    """
    if json_format is None:
        json_format = not sys.stderr.isatty()

    is_test = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    cache_logger = not is_test

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _redact_secrets_processor,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_format
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            path, maxBytes=100 * 1024 * 1024, backupCount=7, encoding="utf-8"
        )
        handler.setLevel(level.upper())
        # We need a stdlib formatter that bridges to structlog
        formatter = structlog.stdlib.ProcessorFormatter(
            processor=renderer, foreign_pre_chain=shared_processors
        )
        handler.setFormatter(formatter)

        logging.basicConfig(
            level=level.upper(),
            handlers=[handler, logging.StreamHandler(sys.stderr)],
            force=True,
        )

        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                *shared_processors,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            wrapper_class=structlog.make_filtering_bound_logger(
                logging.getLevelName(level.upper())
            ),
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=cache_logger,
        )
    else:
        structlog.configure(
            processors=[*shared_processors, renderer],
            wrapper_class=structlog.make_filtering_bound_logger(
                logging.getLevelName(level.upper())
            ),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
            cache_logger_on_first_use=cache_logger,
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
