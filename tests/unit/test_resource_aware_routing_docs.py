"""Resource-aware routing documentation contract checks."""

from pathlib import Path

ROUTING_DOC = Path("docs/getting-started/resource-aware-routing.md")
COVERAGE_DOC = Path("docs/community/documentation-coverage.md")
MKDOCS = Path("mkdocs.yml")


def test_resource_aware_routing_is_discoverable_from_getting_started_nav() -> None:
    mkdocs = MKDOCS.read_text(encoding="utf-8")

    assert "Resource-aware routing: getting-started/resource-aware-routing.md" in mkdocs


def test_resource_aware_routing_covers_current_operator_surfaces() -> None:
    doc = ROUTING_DOC.read_text(encoding="utf-8")

    assert "fallback_backend" in doc
    assert "fallback_threshold_percent" in doc
    assert "per_task_limit_usd" in doc
    assert "maxwell-daemon cost" in doc
    assert "/api/v1/cost" in doc
    assert "/api/v1/issues/dispatch" in doc
    assert "maxwell-daemon fleet nodes" in doc


def test_resource_aware_routing_documents_boundaries_without_overstatement() -> None:
    doc = ROUTING_DOC.read_text(encoding="utf-8")
    normalized = " ".join(doc.split())

    assert "does not yet automatically feed live monthly utilisation" in doc
    assert "Treat `fallback_backend` as a router contract" in doc
    assert "ResourceBroker" in doc
    assert "to_dict()` excludes account secrets" in normalized
    assert "fail closed when quota data is stale" in doc


def test_documentation_coverage_tracks_resource_routing_remaining_gate() -> None:
    coverage = COVERAGE_DOC.read_text(encoding="utf-8")

    assert "`resource-aware-routing.md`" in coverage
    assert "resource-aware routing walkthrough is shipped" in coverage
    assert "fleet issue queue walkthrough is shipped" in coverage
