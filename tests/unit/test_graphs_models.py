"""Task graph model validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from maxwell_daemon.graphs import (
    AgentRole,
    GraphNode,
    NodeRun,
    NodeRunStatus,
    TaskGraph,
)


def _node(
    node_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    max_retries: int = 1,
) -> GraphNode:
    return GraphNode(
        id=node_id,
        role=AgentRole.IMPLEMENTER,
        description=f"{node_id} work",
        depends_on=depends_on,
        max_retries=max_retries,
    )


def test_duplicate_node_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate node id"):
        TaskGraph(id="graph-1", name="delivery", nodes=(_node("plan"), _node("plan")))


def test_missing_dependencies_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown dependency"):
        TaskGraph(id="graph-1", name="delivery", nodes=(_node("qa", depends_on=("missing",)),))


def test_dependency_cycles_are_rejected() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        TaskGraph(
            id="graph-1",
            name="delivery",
            nodes=(
                _node("a", depends_on=("b",)),
                _node("b", depends_on=("a",)),
            ),
        )


def test_self_cycles_are_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot depend on itself"):
        TaskGraph(id="graph-1", name="delivery", nodes=(_node("a", depends_on=("a",)),))


@pytest.mark.parametrize("max_retries", [-1, 6])
def test_invalid_retry_values_are_rejected(max_retries: int) -> None:
    with pytest.raises(ValidationError):
        _node("implement", max_retries=max_retries)


def test_nodes_are_returned_in_deterministic_dependency_order() -> None:
    graph = TaskGraph(
        id="graph-1",
        name="delivery",
        nodes=(
            _node("review", depends_on=("qa",)),
            _node("qa", depends_on=("plan",)),
            _node("plan"),
        ),
    )

    assert [node.id for node in graph.nodes_in_dependency_order()] == [
        "plan",
        "qa",
        "review",
    ]


def test_node_run_tracks_status_and_attempts() -> None:
    run = NodeRun(
        graph_id="graph-1",
        node_id="qa",
        status=NodeRunStatus.SUCCEEDED,
        attempts=1,
        output_ref="artifact:test-result",
    )

    assert run.status is NodeRunStatus.SUCCEEDED
    assert run.output_ref == "artifact:test-result"
