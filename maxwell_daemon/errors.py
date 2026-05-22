"""Typed exception tree for the daemon's HTTP and persistence boundaries.

This module is the foundation of Phase 1.2 of the production-grade epic
(#896). The goal is to replace the ~128 ``except Exception`` sites that
currently collapse every failure to ``HTTPException(409, str(exc))`` at the
HTTP boundary (see ``docs/reviews/2026-05-22-adversarial-review.md`` §4) with
a tree where each subclass carries:

* a stable **HTTP status code** (class attribute — no per-instance branching)
* a stable **problem type URI** (RFC 7807 ``type`` field, machine-readable)
* an **RFC 7807 serializer** that the FastAPI handler invokes uniformly

The handler in ``maxwell_daemon.api.problem`` translates any
:class:`MaxwellError` into ``application/problem+json``. Catching the root
:class:`MaxwellError` is sufficient — LSP guarantees the subclass's status
and type land in the response.

Design notes:

* **DbC** — class attributes are immutable (instances never shadow them);
  ``to_problem_detail`` postcondition is enforced by the unit tests.
* **DRY** — the handler does not branch on subclass; it calls one method.
* **LoD** — the handler only talks to ``error.to_problem_detail()`` and
  ``type(error).http_status``; it does not reach into the error's internals.

Migration plan: existing exceptions (``BudgetExceededError`` in ``core/budget.py``,
``DuplicateTaskIdError`` in ``daemon/task_models.py``, etc.) will be migrated to
inherit from the appropriate node in this tree in follow-up PRs. The tree is
introduced first so call sites have something to subclass from.
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

__all__ = [
    "BackendUnavailableError",
    "BudgetExceededError",
    "ClientError",
    "MaxwellError",
    "PolicyDeniedError",
    "ServerError",
    "StorageError",
    "ValidationFailedError",
    "problem_detail",
]

# RFC 7807 §4.2 — "about:blank" is the canonical no-specific-type URI.
_PROBLEM_TYPE_DEFAULT: Final[str] = "about:blank"

# Namespace for Maxwell-Daemon problem type URIs. These are *identifiers*, not
# URLs that need to resolve; we point them at a stable docs anchor so a future
# operator browsing a problem+json response has somewhere to look.
_PROBLEM_TYPE_BASE: Final[str] = "https://maxwell-daemon.dev/problems/"


class MaxwellError(RuntimeError):
    """Root of the daemon's typed exception tree.

    Subclassing :class:`RuntimeError` (rather than :class:`Exception`) signals
    "this is an operational condition, not a programming bug" — failing
    contracts and assertion errors stay in their own lane.

    Subclasses **must** override :attr:`http_status` and :attr:`problem_type`
    as class attributes. The base values map to a 500 with the RFC 7807
    default ``about:blank`` type, which is safe but useless to clients.
    """

    #: HTTP status code returned when this error escapes to the FastAPI layer.
    http_status: ClassVar[int] = 500

    #: RFC 7807 ``type`` URI identifying the *kind* of problem.
    problem_type: ClassVar[str] = _PROBLEM_TYPE_DEFAULT

    #: Short, human-readable title — RFC 7807 ``title`` field. Set per subclass.
    problem_title: ClassVar[str] = "Internal Error"

    def __init__(self, message: str, *, extras: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        # ``extras`` is stored on the instance (not the class) because it is the
        # only per-raise state the error carries. Wrapping in dict() defensively
        # so a caller mutating the input dict after raise can't poison the body.
        self._extras: dict[str, Any] = dict(extras) if extras else {}

    def to_problem_detail(self) -> dict[str, Any]:
        """Serialise this error to an RFC 7807 ``problem+json`` body.

        Postcondition: the returned dict contains ``type``, ``title``,
        ``status``, ``detail`` keys with stable types. ``extras`` are merged
        in but cannot override the required keys (invariant: a caller can't
        accidentally lie about the status code via ``extras``).
        """
        cls = type(self)
        body: dict[str, Any] = dict(self._extras)
        # Required keys win unconditionally — see TestProblemDetail in tests.
        body["type"] = cls.problem_type
        body["title"] = cls.problem_title
        body["status"] = cls.http_status
        body["detail"] = str(self)
        return body


# ── Client-side problem family (4xx) ─────────────────────────────────────────


class ClientError(MaxwellError):
    """The caller did something wrong. 4xx family.

    Subclassing this signals "do not retry without changing the request" to
    automation. Operators alerting on 4xx storms should filter by
    ``problem_type`` to distinguish validation noise from real policy denials.
    """

    http_status = 400
    problem_type = _PROBLEM_TYPE_BASE + "client-error"
    problem_title = "Bad Request"


class ValidationFailedError(ClientError):
    """Request payload failed Pydantic / business-rule validation."""

    http_status = 422
    problem_type = _PROBLEM_TYPE_BASE + "validation-failed"
    problem_title = "Validation Failed"


class BudgetExceededError(ClientError):
    """Caller's cost budget has been consumed; refuse to dispatch.

    402 Payment Required is the semantically correct status code (RFC 9110
    §15.5.2). Some proxies strip the body; the problem+json detail is what
    operators should rely on.
    """

    http_status = 402
    problem_type = _PROBLEM_TYPE_BASE + "budget-exceeded"
    problem_title = "Budget Exceeded"


class PolicyDeniedError(ClientError):
    """Sandbox/RBAC/gauntlet policy refused the operation. 403 Forbidden."""

    http_status = 403
    problem_type = _PROBLEM_TYPE_BASE + "policy-denied"
    problem_title = "Policy Denied"


# ── Server-side problem family (5xx) ─────────────────────────────────────────


class ServerError(MaxwellError):
    """Something on our side broke. 5xx family.

    Subclassing this signals "retrying may help" to clients with idempotent
    requests. Always logged at WARNING+ by the handler.
    """

    http_status = 500
    problem_type = _PROBLEM_TYPE_BASE + "server-error"
    problem_title = "Internal Server Error"


class BackendUnavailableError(ServerError):
    """A downstream LLM backend (Anthropic/OpenAI/Ollama/...) is unavailable.

    503 invites a client retry with backoff; 502 would suggest a permanent
    upstream failure, which is rarely the actual case for transient outages.
    """

    http_status = 503
    problem_type = _PROBLEM_TYPE_BASE + "backend-unavailable"
    problem_title = "Backend Unavailable"


class StorageError(ServerError):
    """SQLite/Postgres/ledger storage failure.

    Distinct from ``BackendUnavailableError`` because retry semantics differ:
    a storage failure during write may have partially succeeded, so blind
    retry is unsafe. Clients receiving this should reconcile state before
    retrying.
    """

    http_status = 500
    problem_type = _PROBLEM_TYPE_BASE + "storage-error"
    problem_title = "Storage Error"


# ── DRY helper for one-off problem documents ─────────────────────────────────


def problem_detail(
    *,
    status: int,
    title: str,
    detail: str,
    type_: str = _PROBLEM_TYPE_DEFAULT,
    **extras: Any,
) -> dict[str, Any]:
    """Build an RFC 7807 problem+json body without raising an exception.

    Used by the FastAPI handler for legacy ``HTTPException`` paths that
    haven't been migrated to :class:`MaxwellError` yet, and by route
    handlers that want to return a problem+json on a *non-exception* path
    (e.g. partial-success responses).

    Trailing underscores on ``type_`` and ``status_`` extras prevent kwarg
    name conflicts. Caller-supplied ``status_``, ``type_``, ``title_``, or
    ``detail_`` in ``extras`` are silently ignored — the explicit args win.
    """
    body: dict[str, Any] = {
        k.rstrip("_"): v
        for k, v in extras.items()
        if k.rstrip("_") not in {"status", "type", "title", "detail"}
    }
    body["type"] = type_
    body["title"] = title
    body["status"] = status
    body["detail"] = detail
    return body
