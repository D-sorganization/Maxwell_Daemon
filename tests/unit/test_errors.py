"""Tests for the typed exception tree (Phase 1.2-A of the production epic).

The exception tree is the foundation for migrating ~128 ``except Exception``
sites away from generic ``HTTPException(409, str(exc))`` collapses (see
``docs/reviews/2026-05-22-adversarial-review.md`` §4).

Design (DbC):

* Every ``MaxwellError`` carries a stable ``problem_type`` URI and ``http_status``
  *class attribute* — no per-instance branching in the handler.
* ``to_problem_detail()`` returns an RFC 7807 ``problem+json`` body. Postcondition:
  the dict has the four required keys (``type``, ``title``, ``status``, ``detail``).
* Subclass hierarchy is LSP-clean — any ``MaxwellError`` can be caught by any
  ancestor handler.

Test approach: RED → GREEN. These tests are written against an interface that
does not yet exist; ``maxwell_daemon.errors`` will be created to satisfy them.
"""

from __future__ import annotations

import json
from http import HTTPStatus

import pytest

from maxwell_daemon.errors import (
    BackendUnavailableError,
    BudgetExceededError,
    ClientError,
    MaxwellError,
    PolicyDeniedError,
    StorageError,
    ValidationFailedError,
    problem_detail,
)


class TestMaxwellErrorHierarchy:
    """LSP invariants on the exception hierarchy."""

    def test_root_is_a_runtime_error(self) -> None:
        # Subclassing RuntimeError (not Exception) signals "operational, not a bug".
        assert issubclass(MaxwellError, RuntimeError)

    def test_client_errors_are_maxwell_errors(self) -> None:
        assert issubclass(ClientError, MaxwellError)
        assert issubclass(ValidationFailedError, ClientError)
        assert issubclass(BudgetExceededError, ClientError)

    def test_server_errors_are_maxwell_errors(self) -> None:
        assert issubclass(BackendUnavailableError, MaxwellError)
        assert issubclass(StorageError, MaxwellError)

    def test_policy_denied_is_a_client_error(self) -> None:
        # 403 means "you (the caller) did something forbidden" — client-side.
        assert issubclass(PolicyDeniedError, ClientError)

    def test_every_subclass_carries_http_status(self) -> None:
        for cls in (
            ValidationFailedError,
            BudgetExceededError,
            PolicyDeniedError,
            BackendUnavailableError,
            StorageError,
        ):
            assert isinstance(cls.http_status, int), f"{cls.__name__} missing http_status"
            assert HTTPStatus(cls.http_status), f"{cls.__name__}.http_status not a valid HTTP code"

    def test_every_subclass_carries_problem_type(self) -> None:
        for cls in (
            ValidationFailedError,
            BudgetExceededError,
            PolicyDeniedError,
            BackendUnavailableError,
            StorageError,
        ):
            assert isinstance(cls.problem_type, str)
            assert cls.problem_type.startswith(
                "https://"
            ), f"{cls.__name__}.problem_type must be an absolute URI"


class TestStatusCodeMapping:
    """Each error class maps to the right HTTP semantics."""

    @pytest.mark.parametrize(
        ("error_cls", "expected_status"),
        [
            (ValidationFailedError, 422),
            (BudgetExceededError, 402),  # Payment Required — semantically apt for budget.
            (PolicyDeniedError, 403),
            (BackendUnavailableError, 503),
            (StorageError, 500),
        ],
    )
    def test_status_codes(self, error_cls: type[MaxwellError], expected_status: int) -> None:
        assert error_cls.http_status == expected_status


class TestProblemDetail:
    """RFC 7807 serialisation — the public contract of the exception tree."""

    def test_problem_detail_has_required_keys(self) -> None:
        err = ValidationFailedError("prompt is empty")
        body = err.to_problem_detail()
        for key in ("type", "title", "status", "detail"):
            assert key in body, f"problem+json missing {key!r}"

    def test_problem_detail_status_matches_class(self) -> None:
        err = BudgetExceededError("over $5 budget")
        assert err.to_problem_detail()["status"] == 402

    def test_problem_detail_type_matches_class(self) -> None:
        err = PolicyDeniedError("argv not in allowlist")
        body = err.to_problem_detail()
        assert body["type"] == PolicyDeniedError.problem_type

    def test_problem_detail_detail_is_the_message(self) -> None:
        err = StorageError("sqlite locked")
        assert err.to_problem_detail()["detail"] == "sqlite locked"

    def test_problem_detail_is_json_serialisable(self) -> None:
        # Postcondition: the body round-trips through json.dumps without TypeErrors.
        # If a subclass ever adds a non-serialisable extension field, this catches it.
        err = ValidationFailedError("bad input", extras={"field": "prompt", "value": ""})
        body = err.to_problem_detail()
        round_tripped = json.loads(json.dumps(body))
        assert round_tripped == body

    def test_extras_are_merged_into_problem_detail(self) -> None:
        # RFC 7807 allows additional members. ``extras`` lets callers attach
        # structured fields (e.g. ``retry_after``, ``field``) the client can act on.
        err = BudgetExceededError("over budget", extras={"limit_usd": 5.0, "used_usd": 5.42})
        body = err.to_problem_detail()
        assert body["limit_usd"] == 5.0
        assert body["used_usd"] == 5.42

    def test_extras_cannot_override_required_keys(self) -> None:
        # Invariant: extras must not shadow ``type``/``status``/``title``/``detail``.
        # Protects the contract against a caller accidentally lying about status.
        err = ValidationFailedError(
            "bad",
            extras={"status": 999, "type": "https://evil.example/", "detail": "lies"},
        )
        body = err.to_problem_detail()
        assert body["status"] == 422
        assert body["type"] == ValidationFailedError.problem_type
        assert body["detail"] == "bad"


class TestProblemDetailHelper:
    """``problem_detail()`` builds an RFC 7807 dict from arbitrary inputs.

    Used by the FastAPI handler for legacy ``HTTPException``s that haven't
    been migrated yet — provides DRY between typed errors and one-off raises.
    """

    def test_minimal_inputs(self) -> None:
        body = problem_detail(status=404, title="Not Found", detail="nope")
        assert body == {
            "type": "about:blank",  # RFC 7807 default when no specific type.
            "title": "Not Found",
            "status": 404,
            "detail": "nope",
        }

    def test_extras_merge(self) -> None:
        body = problem_detail(status=429, title="Too Many", detail="slow down", retry_after=30)
        assert body["retry_after"] == 30

    def test_required_keys_win_over_extras(self) -> None:
        body = problem_detail(status=500, title="t", detail="d", status_=999)
        # Trailing underscore convention prevents the kwarg conflict; the literal
        # ``status`` key in the output must equal the ``status=`` arg.
        assert body["status"] == 500


class TestImmutability:
    """DbC invariant: error class attributes can't be mutated at runtime."""

    def test_http_status_is_a_class_attribute_not_instance(self) -> None:
        err = ValidationFailedError("x")
        # Instances must not shadow the class-level status — the handler reads
        # ``type(err).http_status``, not ``err.http_status``.
        assert "http_status" not in err.__dict__

    def test_problem_type_is_a_class_attribute_not_instance(self) -> None:
        err = ValidationFailedError("x")
        assert "problem_type" not in err.__dict__
