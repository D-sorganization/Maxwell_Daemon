"""Fleet gauntlet walkthrough documentation contract checks."""

import re
from pathlib import Path

WALKTHROUGH_DOC = Path("docs/getting-started/fleet-gauntlet-walkthrough.md")
COVERAGE_DOC = Path("docs/community/documentation-coverage.md")
MKDOCS = Path("mkdocs.yml")


def test_walkthrough_is_discoverable_from_getting_started_nav() -> None:
    mkdocs = MKDOCS.read_text(encoding="utf-8")

    assert "Fleet gauntlet walkthrough: getting-started/fleet-gauntlet-walkthrough.md" in mkdocs


def test_walkthrough_covers_fleet_memory_gauntlet_and_artifact_surfaces() -> None:
    doc = WALKTHROUGH_DOC.read_text(encoding="utf-8")

    assert "maxwell-daemon fleet nodes" in doc
    assert "/api/v1/fleet/capabilities" in doc
    assert "/api/v1/memory/assemble" in doc
    assert "/api/v1/memory/record" in doc
    assert "/api/v1/control-plane/gauntlet" in doc
    assert "/api/v1/tasks/task-123/artifacts" in doc
    assert "/api/v1/artifacts/artifact-456/content" in doc


def test_walkthrough_documents_retry_waive_and_critic_boundaries() -> None:
    doc = WALKTHROUGH_DOC.read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", doc.lower())

    assert "/api/v1/control-plane/gauntlet/task-123/retry" in doc
    assert "/api/v1/control-plane/gauntlet/task-123/waive" in doc
    assert "waivers preserve the failed task state" in normalized
    assert "Run the Critic Gauntlet" in doc
    assert "home-user path" in doc
    assert "Do not use waivers for flaky or missing gates" in doc


def test_documentation_coverage_tracks_walkthrough_remaining_gates() -> None:
    coverage = COVERAGE_DOC.read_text(encoding="utf-8")

    assert "`fleet-gauntlet-walkthrough.md`" in coverage
    assert "Fleet/shared-memory/critic-gauntlet walkthrough is shipped" in coverage
    assert "resource-aware routing walkthrough is shipped" in coverage
    assert "fleet issue queue walkthrough is shipped" in coverage
