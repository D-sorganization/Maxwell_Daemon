"""WorkItemStore durable persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.core.work_item_store import WorkItemStore
from maxwell_daemon.core.work_items import AcceptanceCriterion, WorkItem, WorkItemStatus


@pytest.fixture
def store(tmp_path: Path) -> WorkItemStore:
    return WorkItemStore(tmp_path / "work_items.db")


def _item(**overrides: object) -> WorkItem:
    data = {"id": "wi-1", "title": "Ship work items"}
    data.update(overrides)  # type: ignore[arg-type]
    return WorkItem(**data)  # type: ignore[arg-type]


def test_create_get_roundtrip(store: WorkItemStore) -> None:
    item = _item(repo="D-sorganization/Maxwell-Daemon")
    store.save(item)

    loaded = store.get(item.id)

    assert loaded is not None
    assert loaded.title == item.title
    assert loaded.repo == "D-sorganization/Maxwell-Daemon"


def test_structured_fields_roundtrip(store: WorkItemStore) -> None:
    item = _item(
        acceptance_criteria=(AcceptanceCriterion(id="AC1", text="has tests"),),
        required_checks=("pytest", "ruff"),
        task_ids=("task-1",),
    )
    store.save(item)

    loaded = store.get(item.id)

    assert loaded is not None
    assert loaded.acceptance_criteria[0].text == "has tests"
    assert loaded.required_checks == ("pytest", "ruff")
    assert loaded.task_ids == ("task-1",)


def test_list_filters_by_status_repo_source_and_priority(store: WorkItemStore) -> None:
    store.save(
        _item(
            id="match",
            repo="D-sorganization/Maxwell-Daemon",
            source="github_issue",
            priority=10,
        )
    )
    store.save(_item(id="other", repo="D-sorganization/Other", source="manual", priority=200))

    listed = store.list_items(
        status=WorkItemStatus.DRAFT,
        repo="D-sorganization/Maxwell-Daemon",
        source="github_issue",
        max_priority=50,
    )

    assert [item.id for item in listed] == ["match"]


def test_transition_updates_timestamps(store: WorkItemStore) -> None:
    item = _item(
        status=WorkItemStatus.REFINED,
        acceptance_criteria=(AcceptanceCriterion(id="AC1", text="has tests"),),
    )
    store.save(item)

    updated = store.transition(item.id, WorkItemStatus.IN_PROGRESS)

    assert updated.status is WorkItemStatus.IN_PROGRESS
    assert updated.started_at is not None
    assert store.get(item.id).started_at is not None  # type: ignore[union-attr]


def test_missing_transition_raises_key_error(store: WorkItemStore) -> None:
    with pytest.raises(KeyError):
        store.transition("missing", WorkItemStatus.CANCELLED)


def test_schema_initialization_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "work_items.db"
    first = WorkItemStore(db)
    first.save(_item())
    second = WorkItemStore(db)

    assert second.get("wi-1") is not None
