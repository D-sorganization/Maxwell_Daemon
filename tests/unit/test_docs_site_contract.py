"""Documentation site contract checks."""

from __future__ import annotations

from pathlib import Path


def test_mkdocs_nav_includes_openapi_reference() -> None:
    mkdocs = Path("mkdocs.yml").read_text(encoding="utf-8")

    assert "OpenAPI: reference/openapi.md" in mkdocs


def test_openapi_docs_explain_schema_and_interactive_routes() -> None:
    doc = Path("docs/reference/openapi.md").read_text(encoding="utf-8")

    assert "GET /openapi.json" in doc
    assert "GET /docs" in doc
    assert "GET /redoc" in doc
    assert "openapi-generator-cli" in doc


def test_rest_api_reference_links_openapi_page() -> None:
    doc = Path("docs/reference/api.md").read_text(encoding="utf-8")

    assert "[OpenAPI reference](openapi.md)" in doc
