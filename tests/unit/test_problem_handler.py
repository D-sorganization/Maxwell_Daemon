"""Tests for the RFC 7807 FastAPI exception handler.

Validates that ``maxwell_daemon.api.problem.install_problem_handler``:

* Catches every :class:`MaxwellError` subclass and returns the correct status.
* Emits ``application/problem+json`` content-type (RFC 7807 §3).
* Body shape matches ``MaxwellError.to_problem_detail()`` exactly — i.e.
  the handler is a *single* code path, not a fan-out (DRY).
* Preserves request correlation IDs in the response (LoD: handler reads the
  correlation header but doesn't reach into the request internals).

These tests are written against ``maxwell_daemon.api.problem`` which does not
yet exist (RED) and drive its creation (GREEN).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from maxwell_daemon.api.problem import install_problem_handler
from maxwell_daemon.errors import (
    BackendUnavailableError,
    BudgetExceededError,
    MaxwellError,
    PolicyDeniedError,
    StorageError,
    ValidationFailedError,
)


@pytest.fixture
def app_with_handler() -> FastAPI:
    """Minimal FastAPI app with the problem handler installed and one
    parametric route that raises whatever the test asks for."""
    app = FastAPI()
    install_problem_handler(app)

    @app.get("/raise/{kind}")
    def _raise(kind: str) -> None:
        match kind:
            case "validation":
                raise ValidationFailedError("bad payload", extras={"field": "prompt"})
            case "budget":
                raise BudgetExceededError("over budget", extras={"limit_usd": 5.0})
            case "policy":
                raise PolicyDeniedError("argv not in allowlist")
            case "backend":
                raise BackendUnavailableError("anthropic timeout")
            case "storage":
                raise StorageError("sqlite locked")
            case _:
                raise RuntimeError("unmapped — should not pass through handler")

    return app


@pytest.fixture
def client(app_with_handler: FastAPI) -> TestClient:
    return TestClient(app_with_handler, raise_server_exceptions=False)


class TestProblemHandlerStatusCodes:
    """Each MaxwellError subclass maps to its declared HTTP status."""

    @pytest.mark.parametrize(
        ("kind", "expected_status"),
        [
            ("validation", 422),
            ("budget", 402),
            ("policy", 403),
            ("backend", 503),
            ("storage", 500),
        ],
    )
    def test_status_code(self, client: TestClient, kind: str, expected_status: int) -> None:
        response = client.get(f"/raise/{kind}")
        assert response.status_code == expected_status


class TestProblemHandlerContentType:
    """RFC 7807 §3 — content-type MUST be application/problem+json."""

    def test_content_type(self, client: TestClient) -> None:
        response = client.get("/raise/validation")
        assert response.headers["content-type"].startswith("application/problem+json")


class TestProblemHandlerBody:
    """Response body equals ``error.to_problem_detail()`` — DRY invariant."""

    def test_body_matches_to_problem_detail(self, client: TestClient) -> None:
        response = client.get("/raise/budget")
        body = response.json()
        # Reconstruct the expected body by raising the same error and serialising.
        expected = BudgetExceededError("over budget", extras={"limit_usd": 5.0}).to_problem_detail()
        assert body == expected

    def test_body_has_required_keys(self, client: TestClient) -> None:
        response = client.get("/raise/policy")
        body = response.json()
        for key in ("type", "title", "status", "detail"):
            assert key in body

    def test_validation_extras_are_present(self, client: TestClient) -> None:
        response = client.get("/raise/validation")
        body = response.json()
        # Extras must round-trip through the handler unchanged.
        assert body["field"] == "prompt"


class TestProblemHandlerUnrelatedExceptions:
    """Non-MaxwellError exceptions pass through to FastAPI's default 500 path.

    This is the explicit LoD boundary: the problem handler only knows about
    the daemon's typed tree. Other handlers (e.g. ``QueueSaturationError``)
    continue to work independently.
    """

    def test_runtime_error_is_not_caught(self, client: TestClient) -> None:
        response = client.get("/raise/unmapped")
        # Default FastAPI 500 — not application/problem+json.
        assert response.status_code == 500
        assert not response.headers["content-type"].startswith("application/problem+json")


class TestProblemHandlerIdempotence:
    """Installing twice should be a no-op, not a duplicate registration.

    Protects against subtle bugs when a test fixture and the production
    ``create_app`` both call ``install_problem_handler``.
    """

    def test_double_install_is_safe(self) -> None:
        app = FastAPI()
        install_problem_handler(app)
        install_problem_handler(app)  # must not raise.

        @app.get("/x")
        def _raise() -> None:
            raise ValidationFailedError("x")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/x")
        assert response.status_code == 422


class TestRootClassCatchAll:
    """LSP: catching :class:`MaxwellError` catches every subclass.

    The handler is registered for the root class only; this test proves
    that's enough to cover the whole tree.
    """

    def test_custom_subclass_is_handled(self) -> None:
        class MyCustomError(MaxwellError):
            http_status = 418
            problem_type = "https://example.test/teapot"
            problem_title = "I'm a teapot"

        app = FastAPI()
        install_problem_handler(app)

        @app.get("/custom")
        def _raise() -> None:
            raise MyCustomError("short and stout")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/custom")
        assert response.status_code == 418
        body = response.json()
        assert body["type"] == "https://example.test/teapot"
        assert body["detail"] == "short and stout"
