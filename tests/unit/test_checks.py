"""Source-controlled Maxwell check loader and local runner tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from maxwell_daemon.checks import (
    CheckConclusion,
    CheckLoadError,
    LocalCheckRunner,
    load_checks,
)
from maxwell_daemon.core.artifacts import ArtifactKind, ArtifactStore


def _write_check(repo: Path, name: str, body: str) -> None:
    check_dir = repo / ".maxwell" / "checks"
    check_dir.mkdir(parents=True, exist_ok=True)
    (check_dir / name).write_text(body, encoding="utf-8")


def test_loads_valid_check_file_deterministically(tmp_path: Path) -> None:
    _write_check(
        tmp_path,
        "scope.md",
        """---
id: scope-drift
name: Scope Drift Review
severity: required
applies_to:
  globs:
    - "**/*.py"
trigger:
  events: [pull_request]
model_tier: moderate
---
Review the diff for scope drift.
""",
    )

    [definition] = load_checks(tmp_path / ".maxwell" / "checks")

    assert definition.id == "scope-drift"
    assert definition.applies_to.globs == ("**/*.py",)
    assert definition.trigger.events == ("pull_request",)
    assert definition.body == "Review the diff for scope drift."
    assert definition.source == tmp_path / ".maxwell" / "checks" / "scope.md"


def test_rejects_malformed_frontmatter(tmp_path: Path) -> None:
    _write_check(tmp_path, "bad.md", "id: missing-frontmatter\n")

    with pytest.raises(CheckLoadError, match="frontmatter"):
        load_checks(tmp_path / ".maxwell" / "checks")


def test_rejects_duplicate_ids(tmp_path: Path) -> None:
    body = """---
id: duplicate
name: Duplicate
applies_to:
  globs: ["**/*"]
trigger:
  events: [pull_request]
---
Check something.
"""
    _write_check(tmp_path, "a.md", body)
    _write_check(tmp_path, "b.md", body)

    with pytest.raises(CheckLoadError, match="duplicate check id"):
        load_checks(tmp_path / ".maxwell" / "checks")


def test_local_runner_matches_and_skips_by_changed_files(tmp_path: Path) -> None:
    _write_check(
        tmp_path,
        "python.md",
        """---
id: python-tests
name: Python Tests
applies_to:
  globs: ["**/*.py"]
trigger:
  events: [pull_request]
---
Verify Python changes have tests.
""",
    )
    _write_check(
        tmp_path,
        "docs.md",
        """---
id: docs
name: Docs
applies_to:
  globs: ["docs/**"]
trigger:
  events: [pull_request]
---
Verify docs changes.
""",
    )

    results = LocalCheckRunner(tmp_path).run(changed_files=("src/app.py",))

    by_id = {result.check_id: result for result in results}
    assert by_id["python-tests"].conclusion is CheckConclusion.PASS
    assert by_id["docs"].conclusion is CheckConclusion.SKIPPED


def test_local_runner_can_persist_structured_results(tmp_path: Path) -> None:
    _write_check(
        tmp_path,
        "scope.md",
        """---
id: scope
name: Scope
applies_to:
  globs: ["**/*"]
trigger:
  events: [pull_request]
---
Verify scope.
""",
    )
    store = ArtifactStore(tmp_path / "artifacts.db", blob_root=tmp_path / "blobs")

    LocalCheckRunner(tmp_path).run(artifact_store=store, work_item_id="wi-1")

    [artifact] = store.list_for_work_item("wi-1", kind=ArtifactKind.CHECK_RESULT)
    payload = json.loads(store.read_text(artifact.id))
    assert payload[0]["check_id"] == "scope"
    assert payload[0]["conclusion"] == "pass"
