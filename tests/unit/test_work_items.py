"""Governed work item model and transition contracts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from maxwell_daemon.core.work_items import (
    AcceptanceCriterion,
    WorkItem,
    WorkItemStatus,
    transition_work_item,
)


def test_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        WorkItem(id="wi-1", title="")


def test_rejects_invalid_repo_format() -> None:
    with pytest.raises(ValidationError):
        WorkItem(id="wi-1", title="x", repo="not-a-full-name")


def test_refined_requires_acceptance_criteria() -> None:
    with pytest.raises(ValidationError, match="acceptance criterion"):
        WorkItem(id="wi-1", title="x", status=WorkItemStatus.REFINED)


def test_invalid_transition_is_rejected() -> None:
    item = WorkItem(id="wi-1", title="x")
    with pytest.raises(ValueError, match="draft -> done"):
        transition_work_item(item, WorkItemStatus.DONE)


def test_transition_to_refined_requires_acceptance_criteria() -> None:
    item = WorkItem(id="wi-1", title="x", status=WorkItemStatus.NEEDS_REFINEMENT)
    with pytest.raises(ValueError, match="acceptance criterion"):
        transition_work_item(item, WorkItemStatus.REFINED)


def test_transition_sets_lifecycle_timestamps() -> None:
    now = datetime.now(timezone.utc)
    item = WorkItem(
        id="wi-1",
        title="x",
        status=WorkItemStatus.REFINED,
        acceptance_criteria=(AcceptanceCriterion(id="AC1", text="verified"),),
    )
    started = transition_work_item(item, WorkItemStatus.IN_PROGRESS, now=now)
    done = transition_work_item(started, WorkItemStatus.DONE, now=now)

    assert started.started_at == now
    assert done.completed_at == now
