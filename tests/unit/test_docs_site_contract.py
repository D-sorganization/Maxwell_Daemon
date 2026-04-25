"""Documentation site contract checks."""

from __future__ import annotations

import re
from pathlib import Path

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig


class _OpenAPIDocsDaemon:
    def __init__(self, config: MaxwellDaemonConfig) -> None:
        self._config = config


def test_mkdocs_nav_includes_openapi_reference() -> None:
    mkdocs = Path("mkdocs.yml").read_text(encoding="utf-8")

    assert "OpenAPI: reference/openapi.md" in mkdocs


def test_mkdocs_nav_includes_grpc_status_reference() -> None:
    mkdocs = Path("mkdocs.yml").read_text(encoding="utf-8")

    assert "gRPC status: reference/grpc.md" in mkdocs


def test_mkdocs_nav_includes_action_ledger_reference() -> None:
    mkdocs = Path("mkdocs.yml").read_text(encoding="utf-8")

    assert "Action ledger: reference/action-ledger.md" in mkdocs


def test_openapi_docs_explain_schema_and_interactive_routes() -> None:
    doc = Path("docs/reference/openapi.md").read_text(encoding="utf-8")

    assert "GET /openapi.json" in doc
    assert "GET /docs" in doc
    assert "GET /redoc" in doc
    assert "openapi-generator-cli" in doc


def test_openapi_route_inventory_matches_live_schema(
    minimal_config: MaxwellDaemonConfig,
) -> None:
    doc = Path("docs/reference/openapi.md").read_text(encoding="utf-8")
    section = doc.split("## Live route inventory", maxsplit=1)[1].split(
        "\n## ", maxsplit=1
    )[0]
    documented_paths = set(re.findall(r"`(/[^`]+)`", section))

    app = create_app(_OpenAPIDocsDaemon(minimal_config))  # type: ignore[arg-type]
    schema_paths = set(app.openapi()["paths"])

    assert documented_paths == schema_paths


def test_grpc_reference_does_not_overstate_unimplemented_transport() -> None:
    doc = Path("docs/reference/grpc.md").read_text(encoding="utf-8")

    assert "does not currently ship a public gRPC service contract" in doc
    assert "No stable `.proto` files are published." in doc
    assert 'pip install "maxwell-daemon[grpc]"' in doc
    assert "python -m grpc_tools.protoc" in doc
    assert "git diff --exit-code" in doc
    assert "roadmap-only" in doc


def test_rest_api_reference_links_openapi_page() -> None:
    doc = Path("docs/reference/api.md").read_text(encoding="utf-8")

    assert "[OpenAPI reference](openapi.md)" in doc


def test_action_ledger_reference_covers_safety_contract() -> None:
    doc = Path("docs/reference/action-ledger.md").read_text(encoding="utf-8")

    assert "suggest" in doc
    assert "auto-edit" in doc
    assert "full-auto" in doc
    assert "action_approved" in doc
    assert "proposal-only" in doc
