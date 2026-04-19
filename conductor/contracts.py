"""Design-by-Contract primitives.

This module provides four tools:

* ``require(cond, msg)`` — precondition at statement level.
* ``ensure(cond, msg)``  — postcondition at statement level.
* ``@precondition(fn, msg)`` — function decorator checking ``fn(*args, **kwargs)`` before call.
* ``@postcondition(fn, msg)`` — function decorator checking ``fn(result)`` after call.
* ``@invariant(fn, msg)`` — class decorator checking ``fn(self)`` after every public method.

Every contract raises a ``ContractViolation`` (``PreconditionError`` /
``PostconditionError``) so callers can catch classes of failures uniformly.

Contracts can be disabled via ``CONDUCTOR_CONTRACTS=off`` for performance-critical
production deployments — but the default is on, because a failing contract in
prod means something worse is already wrong.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import os
from collections.abc import Callable
from typing import Any, TypeVar

__all__ = [
    "ContractViolation",
    "PostconditionError",
    "PreconditionError",
    "contracts_enabled",
    "ensure",
    "invariant",
    "postcondition",
    "precondition",
    "require",
]

F = TypeVar("F", bound=Callable[..., Any])
C = TypeVar("C", bound=type)


class ContractViolation(AssertionError):  # noqa: N818  — "Violation" conveys contract semantics more clearly than "Error"
    """Base class for all contract failures."""


class PreconditionError(ContractViolation):
    """A precondition was not met when a function was called."""


class PostconditionError(ContractViolation):
    """A postcondition was not met by a function's result or final state."""


def contracts_enabled() -> bool:
    """Read the env var fresh every call so tests can toggle at runtime."""
    return os.environ.get("CONDUCTOR_CONTRACTS", "on").lower() != "off"


def require(condition: bool, message: str) -> None:
    if contracts_enabled() and not condition:
        raise PreconditionError(message)


def ensure(condition: bool, message: str) -> None:
    if contracts_enabled() and not condition:
        raise PostconditionError(message)


def precondition(check: Callable[..., bool], message: str) -> Callable[[F], F]:
    """Decorator — verify ``check(*args, **kwargs)`` returns truthy before the call."""

    def decorator(func: F) -> F:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if contracts_enabled() and not check(*args, **kwargs):
                    raise PreconditionError(f"{func.__qualname__}: {message}")
                return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            if contracts_enabled() and not check(*args, **kwargs):
                raise PreconditionError(f"{func.__qualname__}: {message}")
            return func(*args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def postcondition(check: Callable[[Any], bool], message: str) -> Callable[[F], F]:
    """Decorator — verify ``check(result)`` returns truthy after the call."""

    def decorator(func: F) -> F:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                result = await func(*args, **kwargs)
                if contracts_enabled() and not check(result):
                    raise PostconditionError(f"{func.__qualname__}: {message}")
                return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            result = func(*args, **kwargs)
            if contracts_enabled() and not check(result):
                raise PostconditionError(f"{func.__qualname__}: {message}")
            return result

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def invariant(check: Callable[[Any], bool], message: str) -> Callable[[C], C]:
    """Class decorator — verify ``check(self)`` after every public method.

    Public methods are those whose names don't start with ``_``. This lets an
    object temporarily break its invariant inside private helpers as long as the
    invariant holds whenever control returns to the caller.
    """

    def decorator(cls: C) -> C:
        for name, member in list(vars(cls).items()):
            if name.startswith("_"):
                continue
            if not callable(member) or isinstance(member, (staticmethod, classmethod)):
                continue
            if inspect.iscoroutinefunction(member):
                setattr(cls, name, _wrap_async_invariant(member, check, message))
            else:
                setattr(cls, name, _wrap_sync_invariant(member, check, message))
        return cls

    return decorator


def _wrap_sync_invariant(
    method: Callable[..., Any],
    check: Callable[[Any], bool],
    message: str,
) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = method(self, *args, **kwargs)
        if contracts_enabled() and not check(self):
            raise ContractViolation(
                f"{type(self).__name__}.{method.__name__}: invariant violated — {message}"
            )
        return result

    return wrapper


def _wrap_async_invariant(
    method: Callable[..., Any],
    check: Callable[[Any], bool],
    message: str,
) -> Callable[..., Any]:
    @functools.wraps(method)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await method(self, *args, **kwargs)
        if contracts_enabled() and not check(self):
            raise ContractViolation(
                f"{type(self).__name__}.{method.__name__}: invariant violated — {message}"
            )
        return result

    return wrapper
