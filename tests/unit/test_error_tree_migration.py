"""Tests for migrating domain exceptions onto the typed error tree (#896, Phase 1.2).

Phase 1.2 introduced :mod:`maxwell_daemon.errors` (the ``MaxwellError`` tree) and
:mod:`maxwell_daemon.api.problem` (the single RFC 7807 handler), but the tree
shipped *empty of real callers*: every domain exception still subclassed bare
``Exception`` / ``RuntimeError`` / ``ValueError`` and was translated to HTTP by
ad-hoc, per-route handlers. This module drives the first migration wave:

* ``QueueSaturationError`` (429 + ``Retry-After``) joins the tree via a new
  :class:`~maxwell_daemon.errors.RateLimitedError` node.
* ``DuplicateTaskIdError`` (409) joins the tree via a new
  :class:`~maxwell_daemon.errors.ConflictError` node.

Both must satisfy two contracts simultaneously:

1. **HTTP contract** — the unified RFC 7807 handler now governs them, so the
   bespoke ``server.py`` / ``dispatch.py`` translation sites can be deleted
   (DRY). Status codes and the ``Retry-After`` header must be unchanged.
2. **Backwards-compat catch contract** — existing code catches
   ``QueueSaturationError`` as ``Exception`` and ``DuplicateTaskIdError`` as
   ``ValueError``. Migration must not break those ``except`` clauses (LSP).

Written RED-first against behaviour that does not yet exist.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from maxwell_daemon.api.problem import PROBLEM_JSON_MEDIA_TYPE, install_problem_handler
from maxwell_daemon.daemon.task_models import DuplicateTaskIdError, QueueSaturationError
from maxwell_daemon.errors import (
    ClientError,
    ConflictError,
    MaxwellError,
    RateLimitedError,
)


class TestErrorTreeNodes:
    """The new intermediate nodes carry the right status + headers contract."""

    def test_rate_limited_is_a_client_error(self) -> None:
        # 429 is a client-side condition; it must sit under ClientError so
        # `except ClientError` automation catches it.
        assert issubclass(RateLimitedError, ClientError)
        assert RateLimitedError.http_status == 429

    def test_conflict_is_a_client_error(self) -> None:
        assert issubclass(ConflictError, ClientError)
        assert ConflictError.http_status == 409

    def test_base_error_has_no_response_headers(self) -> None:
        # Default contract: a plain MaxwellError contributes no extra headers.
        assert MaxwellError("x").response_headers() == {}

    def test_rate_limited_emits_retry_after_header(self) -> None:
        err = RateLimitedError("slow down", retry_after_seconds=42)
        assert err.response_headers() == {"Retry-After": "42"}

    def test_rate_limited_retry_after_is_in_problem_body(self) -> None:
        # The machine-readable body should also surface the hint for clients
        # that read JSON rather than headers.
        body = RateLimitedError("slow down", retry_after_seconds=42).to_problem_detail()
        assert body["retry_after_seconds"] == 42
        assert body["status"] == 429


class TestQueueSaturationMigration:
    """``QueueSaturationError`` is now a ``RateLimitedError`` (LSP-preserving)."""

    def test_is_maxwell_error(self) -> None:
        assert issubclass(QueueSaturationError, MaxwellError)
        assert issubclass(QueueSaturationError, RateLimitedError)

    def test_still_catchable_as_exception(self) -> None:
        # Existing daemon code does `except QueueSaturationError` and relies on
        # it being an ordinary Exception. MaxwellError -> RuntimeError -> Exception.
        assert issubclass(QueueSaturationError, Exception)

    def test_backoff_seconds_attribute_preserved(self) -> None:
        # The historical attribute name and default must not change — callers
        # (and the retry policy tests) read `.backoff_seconds`.
        err = QueueSaturationError("full")
        assert err.backoff_seconds == 60
        assert QueueSaturationError("full", backoff_seconds=15).backoff_seconds == 15

    def test_maps_to_429_with_retry_after(self) -> None:
        err = QueueSaturationError("full", backoff_seconds=30)
        assert err.http_status == 429
        assert err.response_headers() == {"Retry-After": "30"}


class TestDuplicateTaskIdMigration:
    """``DuplicateTaskIdError`` is now a ``ConflictError`` but stays a ``ValueError``."""

    def test_is_maxwell_error(self) -> None:
        assert issubclass(DuplicateTaskIdError, MaxwellError)
        assert issubclass(DuplicateTaskIdError, ConflictError)

    def test_still_catchable_as_value_error(self) -> None:
        # tasks.py has `except ValueError` fallbacks that must keep working.
        assert issubclass(DuplicateTaskIdError, ValueError)
        with pytest.raises(ValueError):
            raise DuplicateTaskIdError("dupe")

    def test_maps_to_409(self) -> None:
        assert DuplicateTaskIdError("dupe").http_status == 409


class TestHandlerAppliesResponseHeaders:
    """The single RFC 7807 handler now forwards ``response_headers()``."""

    @pytest.fixture
    def client(self) -> TestClient:
        app = FastAPI()
        install_problem_handler(app)

        @app.get("/saturate")
        def _saturate() -> None:
            raise QueueSaturationError("queue full", backoff_seconds=17)

        @app.get("/dupe")
        def _dupe() -> None:
            raise DuplicateTaskIdError("id already exists")

        return TestClient(app, raise_server_exceptions=False)

    def test_queue_saturation_returns_429_problem_json(self, client: TestClient) -> None:
        resp = client.get("/saturate")
        assert resp.status_code == 429
        assert resp.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)

    def test_queue_saturation_sets_retry_after_header(self, client: TestClient) -> None:
        resp = client.get("/saturate")
        assert resp.headers["retry-after"] == "17"

    def test_duplicate_task_returns_409_problem_json(self, client: TestClient) -> None:
        resp = client.get("/dupe")
        assert resp.status_code == 409
        assert resp.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
        assert resp.json()["detail"] == "id already exists"
