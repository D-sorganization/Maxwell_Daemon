"""Fleet issue queue walkthrough documentation contract checks."""

from pathlib import Path

COVERAGE_DOC = Path("docs/community/documentation-coverage.md")
MKDOCS = Path("mkdocs.yml")
WALKTHROUGH_DOC = Path("docs/getting-started/fleet-issue-queue.md")


def test_fleet_issue_queue_is_discoverable_from_getting_started_nav() -> None:
    mkdocs = MKDOCS.read_text(encoding="utf-8")

    assert "Fleet issue queue: getting-started/fleet-issue-queue.md" in mkdocs


def test_fleet_issue_queue_covers_current_operator_surfaces() -> None:
    doc = WALKTHROUGH_DOC.read_text(encoding="utf-8")

    assert "maxwell-daemon issue dispatch-batch" in doc
    assert "--dry-run" in doc
    assert "--max-stories" in doc
    assert "--label" in doc
    assert "--all" in doc
    assert "--fleet-manifest" in doc
    assert "/api/v1/issues/batch-dispatch" in doc
    assert "DiscoveryScheduler" in doc
    assert "DiscoveryRepoSpec" in doc
    assert "discovery_dedup.json" in doc


def test_fleet_issue_queue_documents_safety_boundaries() -> None:
    doc = WALKTHROUGH_DOC.read_text(encoding="utf-8")
    normalized = " ".join(doc.lower().split())

    assert "run `--dry-run` for every new queue shape" in normalized
    assert "prefer `plan` mode first" in normalized
    assert "per-repo safety cap" in normalized
    assert "one broken repository does not strand the rest of the fleet" in normalized
    assert "do not treat the issue queue as a merge queue" in normalized
    assert "do not auto-merge issue prs from queue intake alone" in normalized
    assert "critic review" in normalized
    assert "human or ci gates" in normalized


def test_documentation_coverage_tracks_fleet_issue_queue_gate() -> None:
    coverage = COVERAGE_DOC.read_text(encoding="utf-8")

    assert "`fleet-issue-queue.md`" in coverage
    assert "fleet issue queue walkthrough is shipped" in coverage
    assert "dry-run batch dispatch" in coverage
    assert "scheduler dedup boundaries" in coverage
