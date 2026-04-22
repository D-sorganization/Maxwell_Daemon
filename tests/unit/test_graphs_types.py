"""Task graph first-slice tests for issue #286."""

from __future__ import annotations

import pytest

from maxwell_daemon.contracts import PreconditionError
from maxwell_daemon.graphs.types import (
    AgentRole,
    GraphTemplateKind,
    GraphTemplateSelectorInput,
    HandoffArtifactKind,
    TaskGraph,
    TaskGraphNode,
    select_template_kind,
    template_for_kind,
)


def _planner() -> TaskGraphNode:
    return TaskGraphNode(
        id="planner",
        role=AgentRole.PLANNER,
        output_artifact_kind=HandoffArtifactKind.PLAN,
    )


def _implementer(depends_on: tuple[str, ...] = ("planner",)) -> TaskGraphNode:
    return TaskGraphNode(
        id="implementer",
        role=AgentRole.IMPLEMENTER,
        output_artifact_kind=HandoffArtifactKind.IMPLEMENTATION_DIFF,
        depends_on=depends_on,
        required_artifacts=(HandoffArtifactKind.PLAN,),
    )


def test_task_graph_happy_path_validates() -> None:
    graph = TaskGraph(id="g1", work_item_id="wi1", nodes=(_planner(), _implementer()))
    graph.validate()


def test_task_graph_rejects_duplicate_node_ids() -> None:
    node = _planner()
    graph = TaskGraph(id="g1", work_item_id="wi1", nodes=(node, node))
    with pytest.raises(PreconditionError, match="duplicate node id"):
        graph.validate()


def test_task_graph_rejects_unknown_dependency() -> None:
    graph = TaskGraph(
        id="g1",
        work_item_id="wi1",
        nodes=(
            _planner(),
            TaskGraphNode(
                id="qa",
                role=AgentRole.QA,
                output_artifact_kind=HandoffArtifactKind.QA_REPORT,
                depends_on=("ghost",),
            ),
        ),
    )
    with pytest.raises(PreconditionError, match="unknown node"):
        graph.validate()


def test_task_graph_rejects_cycle() -> None:
    graph = TaskGraph(
        id="g1",
        work_item_id="wi1",
        nodes=(
            TaskGraphNode(
                id="planner",
                role=AgentRole.PLANNER,
                output_artifact_kind=HandoffArtifactKind.PLAN,
                depends_on=("implementer",),
            ),
            TaskGraphNode(
                id="implementer",
                role=AgentRole.IMPLEMENTER,
                output_artifact_kind=HandoffArtifactKind.IMPLEMENTATION_DIFF,
                depends_on=("planner",),
                required_artifacts=(HandoffArtifactKind.PLAN,),
            ),
        ),
    )
    with pytest.raises(PreconditionError, match="cycle"):
        graph.validate()


def test_task_graph_rejects_invalid_retry_value() -> None:
    graph = TaskGraph(
        id="g1",
        work_item_id="wi1",
        nodes=(
            TaskGraphNode(
                id="planner",
                role=AgentRole.PLANNER,
                output_artifact_kind=HandoffArtifactKind.PLAN,
                max_retries=-1,
            ),
        ),
    )
    with pytest.raises(PreconditionError, match="invalid max_retries"):
        graph.validate()


def test_task_graph_rejects_missing_required_artifact() -> None:
    graph = TaskGraph(
        id="g1",
        work_item_id="wi1",
        nodes=(
            TaskGraphNode(
                id="planner",
                role=AgentRole.PLANNER,
                output_artifact_kind=HandoffArtifactKind.PLAN,
            ),
            TaskGraphNode(
                id="qa",
                role=AgentRole.QA,
                output_artifact_kind=HandoffArtifactKind.QA_REPORT,
                depends_on=("planner",),
                required_artifacts=(HandoffArtifactKind.IMPLEMENTATION_DIFF,),
            ),
        ),
    )
    with pytest.raises(PreconditionError, match="requires missing artifact"):
        graph.validate()


def test_ready_node_ids_returns_only_satisfied_nodes() -> None:
    graph = TaskGraph(id="g1", work_item_id="wi1", nodes=(_planner(), _implementer()))
    assert graph.ready_node_ids(set()) == ("planner",)
    assert graph.ready_node_ids({"planner"}) == ("implementer",)


def test_template_selector_micro_delivery_for_low_risk_small_ac() -> None:
    selected = select_template_kind(
        GraphTemplateSelectorInput(
            acceptance_criteria_count=2,
            risk_level="low",
            has_security_labels=False,
        )
    )
    assert selected is GraphTemplateKind.MICRO_DELIVERY


def test_template_selector_security_sensitive_when_high_risk() -> None:
    selected = select_template_kind(
        GraphTemplateSelectorInput(
            acceptance_criteria_count=1,
            risk_level="critical",
            has_security_labels=False,
        )
    )
    assert selected is GraphTemplateKind.SECURITY_SENSITIVE_DELIVERY


def test_template_selector_standard_for_normal_work() -> None:
    selected = select_template_kind(
        GraphTemplateSelectorInput(
            acceptance_criteria_count=4,
            risk_level="medium",
            has_security_labels=False,
        )
    )
    assert selected is GraphTemplateKind.STANDARD_DELIVERY


def test_all_built_in_templates_instantiate_valid_graphs() -> None:
    for kind in (
        GraphTemplateKind.MICRO_DELIVERY,
        GraphTemplateKind.STANDARD_DELIVERY,
        GraphTemplateKind.SECURITY_SENSITIVE_DELIVERY,
    ):
        template = template_for_kind(kind)
        graph = template.instantiate(graph_id=f"g-{kind.value}", work_item_id="wi1")
        graph.validate()
