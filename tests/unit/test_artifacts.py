"""Durable artifact store contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from maxwell_daemon.core.artifacts import (
    Artifact,
    ArtifactIntegrityError,
    ArtifactKind,
    ArtifactStore,
)


def _store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts.db", blob_root=tmp_path / "blobs")


def test_artifact_requires_exactly_one_owner(tmp_path: Path) -> None:
    artifact = _store(tmp_path).put_text(
        task_id="task-1",
        kind=ArtifactKind.PLAN,
        name="Plan",
        text="Ship it",
    )

    assert artifact.task_id == "task-1"
    assert artifact.work_item_id is None

    with pytest.raises(ValidationError, match="exactly one task or work item"):
        Artifact.model_validate(artifact.model_dump() | {"task_id": None})

    with pytest.raises(ValueError, match="both task and work item"):
        _store(tmp_path).put_text(
            task_id="task-1",
            work_item_id="wi-1",
            kind=ArtifactKind.PLAN,
            name="Bad",
            text="no",
        )


def test_text_artifact_round_trips_with_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)

    artifact = store.put_text(
        task_id="task-1",
        kind=ArtifactKind.PR_BODY,
        name="PR body",
        text="Closes #1",
        media_type="text/markdown",
        metadata={"repo": "owner/repo"},
    )

    loaded = store.get(artifact.id)
    assert loaded == artifact
    assert store.read_text(artifact.id) == "Closes #1"
    assert artifact.sha256
    assert artifact.size_bytes == len(b"Closes #1")
    assert artifact.path.as_posix().startswith("tasks/task-1/")


def test_json_artifact_round_trips_with_sorted_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)

    artifact = store.put_json(
        task_id="task-1",
        kind=ArtifactKind.SANDBOX_EXECUTION,
        name="Sandbox execution",
        value={"b": 2, "a": 1},
        metadata={"gate": "lint"},
    )

    assert artifact.media_type == "application/json"
    assert store.read_text(artifact.id) == '{"a":1,"b":2}'
    assert artifact.kind is ArtifactKind.SANDBOX_EXECUTION


def test_binary_artifact_persists_across_store_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "artifacts.db"
    blob_root = tmp_path / "blobs"
    first = ArtifactStore(db_path, blob_root=blob_root)
    artifact = first.put_bytes(
        work_item_id="wi-1",
        kind=ArtifactKind.SCREENSHOT,
        name="Screenshot",
        data=b"\x89PNG bytes",
        media_type="image/png",
    )

    second = ArtifactStore(db_path, blob_root=blob_root)

    assert second.get(artifact.id) == artifact
    assert second.read_bytes(artifact.id) == b"\x89PNG bytes"


def test_lists_by_owner_and_kind_deterministically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.put_text(
        task_id="task-1", kind=ArtifactKind.PLAN, name="Plan", text="one"
    )
    store.put_text(task_id="other", kind=ArtifactKind.PLAN, name="Other", text="other")
    second = store.put_text(
        task_id="task-1", kind=ArtifactKind.DIFF, name="Diff", text="two"
    )

    assert [item.id for item in store.list_for_task("task-1")] == [first.id, second.id]
    assert [
        item.id for item in store.list_for_task("task-1", kind=ArtifactKind.DIFF)
    ] == [second.id]


def test_rejects_tampered_blob_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = store.put_text(
        task_id="task-1", kind=ArtifactKind.PLAN, name="Plan", text="safe"
    )
    with sqlite3.connect(tmp_path / "artifacts.db") as conn:
        conn.execute(
            "UPDATE artifacts SET path = ? WHERE id = ?", ("../escape.txt", artifact.id)
        )

    with pytest.raises(ArtifactIntegrityError, match="escapes blob root"):
        store.read_text(artifact.id)


def test_detects_corrupted_blob_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = store.put_text(
        task_id="task-1", kind=ArtifactKind.PLAN, name="Plan", text="safe"
    )
    (tmp_path / "blobs" / artifact.path).write_text("changed", encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match="integrity check"):
        store.read_text(artifact.id)
