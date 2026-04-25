"""GAAI backlog loader and mapper foundation contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.core.artifacts import ArtifactKind
from maxwell_daemon.core.work_items import WorkItemStatus
from maxwell_daemon.gaai import (
    GaaiLoadError,
    load_gaai_item,
    load_gaai_items,
    map_gaai_artifacts,
    map_gaai_item_to_work_item,
)


def test_loads_yaml_backlog_item_and_maps_work_item(tmp_path: Path) -> None:
    item_path = tmp_path / "story.yaml"
    item_path.write_text(
        """
id: GAAI-285
title: Governed backlog import
description: Import governed backlog metadata without daemon wiring.
repo: D-sorganization/Maxwell-Daemon
source_url: https://example.test/stories/GAAI-285
acceptance_criteria:
  - Loader parses local YAML.
  - Mapper emits a Maxwell work item.
required_checks:
  - python -m pytest tests/unit/test_gaai.py -q
scope:
  allowed_paths:
    - maxwell_daemon/gaai
  denied_paths:
    - maxwell_daemon/graphs
  allowed_commands:
    - python -m pytest
  risk_level: low
priority: 25
""".lstrip(),
        encoding="utf-8",
    )

    item = load_gaai_item(item_path, root=tmp_path)
    work_item = map_gaai_item_to_work_item(item)

    assert work_item.id == "GAAI-285"
    assert work_item.source == "gaai"
    assert work_item.status is WorkItemStatus.DRAFT
    assert work_item.acceptance_criteria[0].id == "AC1"
    assert work_item.acceptance_criteria[1].text == "Mapper emits a Maxwell work item."
    assert work_item.required_checks == ("python -m pytest tests/unit/test_gaai.py -q",)
    assert work_item.scope.allowed_paths == ("maxwell_daemon/gaai",)
    assert work_item.scope.denied_paths == ("maxwell_daemon/graphs",)
    assert work_item.priority == 25


def test_loads_markdown_front_matter_and_maps_artifact_imports(tmp_path: Path) -> None:
    item_path = tmp_path / "story.md"
    item_path.write_text(
        """
---
key: GAAI-ART
title: Artifact evidence
repo: D-sorganization/Maxwell-Daemon
artifact_refs:
  - path: evidence/summary.md
    kind: metadata
    name: Evidence summary
  - path: patches/fix.diff
    kind: diff
---
Markdown body from the governed backlog item.
""".lstrip(),
        encoding="utf-8",
    )

    item = load_gaai_item(item_path, root=tmp_path)
    work_item = map_gaai_item_to_work_item(item)
    artifacts = map_gaai_artifacts(item)

    assert work_item.body == "Markdown body from the governed backlog item."
    assert [artifact.source_path.as_posix() for artifact in artifacts] == [
        "evidence/summary.md",
        "patches/fix.diff",
    ]
    assert artifacts[0].kind is ArtifactKind.METADATA
    assert artifacts[0].media_type == "text/markdown"
    assert artifacts[1].kind is ArtifactKind.DIFF
    assert artifacts[1].media_type == "text/x-diff"


def test_directory_loader_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "b.yaml").write_text("id: B\ntitle: Second\n", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "a.yaml").write_text("id: A\ntitle: First\n", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("id: ignored\ntitle: ignored\n", encoding="utf-8")

    items = load_gaai_items(tmp_path)

    assert [item.id for item in items] == ["B", "A"]


def test_directory_loader_skips_markdown_without_front_matter(tmp_path: Path) -> None:
    (tmp_path / "story.md").write_text(
        """
---
id: GAAI-MD
title: Markdown item
---
Structured metadata body.
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Plain documentation\n", encoding="utf-8")

    items = load_gaai_items(tmp_path)

    assert [item.id for item in items] == ["GAAI-MD"]


def test_single_markdown_load_requires_front_matter(tmp_path: Path) -> None:
    item_path = tmp_path / "README.md"
    item_path.write_text("# Plain documentation\n", encoding="utf-8")

    with pytest.raises(GaaiLoadError, match="requires YAML front matter"):
        load_gaai_item(item_path, root=tmp_path)


def test_rejects_metadata_path_that_escapes_root(tmp_path: Path) -> None:
    item_path = tmp_path / "story.yaml"
    item_path.write_text("id: BAD\ntitle: Bad\nartifacts:\n  - ../outside.txt\n", encoding="utf-8")

    with pytest.raises(GaaiLoadError, match="relative and contained"):
        load_gaai_item(item_path, root=tmp_path)


def test_rejects_file_reads_outside_configured_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-gaai.yaml"
    outside.write_text("id: OUT\ntitle: Outside\n", encoding="utf-8")
    try:
        with pytest.raises(GaaiLoadError, match="escapes root"):
            load_gaai_item(outside, root=tmp_path)
    finally:
        outside.unlink(missing_ok=True)
