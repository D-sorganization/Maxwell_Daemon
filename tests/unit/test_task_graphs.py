"""Task graph schema, template, and runner contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from maxwell_daemon.core.artifacts import ArtifactKind, ArtifactStore
from maxwell_daemon.core.work_items import AcceptanceCriterion, ScopeBoundary, WorkItem
from maxwell_daemon.director.task_graph_runner import (
    GraphExecutionContext,
    GraphNodeOutput,
    GraphRunner,
)
from maxwell_daemon.director.task_graphs import (
    AgentRole,
    GraphNode,
    GraphStatus,
    NodeRun,
    NodeRunStatus,
    TaskGraph,
    TaskGraphTemplate,
    build_task_graph,
    select_task_graph_template,
)


def _node(
    node_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    required_artifacts: tuple[ArtifactKind, ...] = (),
    output: ArtifactKind = ArtifactKind.HANDOFF,
) -> GraphNode:
    return GraphNode(
        id=node_id,
        role=AgentRole.SPECIALIST,
        depends_on=depends_on,
        required_artifacts=required_artifacts,
        output_artifact_kind=output,
    )


def _graph(*nodes: GraphNode) -> TaskGraph:
    return TaskGraph(
        id="graph-1",
        work_item_id="wi-1",
        template=TaskGraphTemplate.STANDARD_DELIVERY,
        nodes=nodes,
    )


def _work_item(
    *,
    risk_level: str = "medium",
    criteria_count: int = 3,
) -> WorkItem:
    criteria = tuple(
        AcceptanceCriterion(id=f"ac-{index}", text=f"criterion {index}")
        for index in range(criteria_count)
    )
    return WorkItem(
        id="wi-1",
        title="Ship graph support",
        acceptance_criteria=criteria,
        scope=ScopeBoundary(risk_level=risk_level),  # type: ignore[arg-type]
    )


class TestGraphValidation:
    def test_duplicate_node_ids_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="node ids must be unique"):
            _graph(_node("planner"), _node("planner"))

    def test_missing_dependency_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown node"):
            _graph(_node("qa", depends_on=("missing",)))

    def test_dependency_cycle_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cycle"):
            _graph(_node("a", depends_on=("b",)), _node("b", depends_on=("a",)))

    def test_self_cycle_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot depend on itself"):
            _graph(_node("a", depends_on=("a",)))

    def test_retry_bounds_are_enforced(self) -> None:
        with pytest.raises(ValidationError):
            GraphNode(
                id="planner",
                role=AgentRole.PLANNER,
                output_artifact_kind=ArtifactKind.PLAN,
                max_retries=-1,
            )
        with pytest.raises(ValidationError):
            GraphNode(
                id="planner",
                role=AgentRole.PLANNER,
                output_artifact_kind=ArtifactKind.PLAN,
                max_retries=6,
            )

    def test_nodes_return_deterministic_dependency_order(self) -> None:
        graph = _graph(
            _node("reviewer", depends_on=("qa",)),
            _node("planner", output=ArtifactKind.PLAN),
            _node("qa", depends_on=("planner",)),
        )

        assert [node.id for node in graph.nodes_in_dependency_order()] == [
            "planner",
            "qa",
            "reviewer",
        ]

    def test_node_run_tracks_artifacts_and_costs(self) -> None:
        run = NodeRun(
            graph_id="graph-1",
            node_id="planner",
            status=NodeRunStatus.COMPLETED,
            artifact_ids=("artifact-1",),
            cost_usd=0.012,
        )

        assert run.artifact_ids == ("artifact-1",)
        assert run.cost_usd == 0.012


class TestTemplates:
    def test_low_risk_small_work_selects_micro_template(self) -> None:
        assert (
            select_task_graph_template(risk_level="low", acceptance_criteria_count=2)
            is TaskGraphTemplate.MICRO_DELIVERY
        )

    def test_default_work_selects_standard_template(self) -> None:
        assert (
            select_task_graph_template(risk_level="medium", acceptance_criteria_count=3)
            is TaskGraphTemplate.STANDARD_DELIVERY
        )

    def test_security_signals_select_security_template(self) -> None:
        assert (
            select_task_graph_template(
                risk_level="medium",
                acceptance_criteria_count=1,
                labels=("Security",),
            )
            is TaskGraphTemplate.SECURITY_SENSITIVE_DELIVERY
        )
        assert (
            select_task_graph_template(risk_level="critical", acceptance_criteria_count=1)
            is TaskGraphTemplate.SECURITY_SENSITIVE_DELIVERY
        )

    def test_build_task_graph_uses_work_item_risk_and_criteria(self) -> None:
        graph = build_task_graph(_work_item(risk_level="low", criteria_count=1), graph_id="g")

        assert graph.id == "g"
        assert graph.template is TaskGraphTemplate.MICRO_DELIVERY
        assert [node.id for node in graph.nodes] == ["implement-qa"]

    def test_standard_template_has_planner_implementer_qa_reviewer(self) -> None:
        graph = build_task_graph(
            _work_item(risk_level="medium", criteria_count=3),
            template=TaskGraphTemplate.STANDARD_DELIVERY,
        )

        assert [node.id for node in graph.nodes] == ["planner", "implementer", "qa", "reviewer"]
        assert graph.nodes[1].required_artifacts == (ArtifactKind.PLAN,)
        assert graph.nodes[2].required_artifacts == (ArtifactKind.DIFF,)


class _RecordingExecutor:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.contexts: list[GraphExecutionContext] = []

    def execute(self, context: GraphExecutionContext) -> GraphNodeOutput:
        self.contexts.append(context)
        return GraphNodeOutput(
            text=self.outputs.pop(0),
            metadata={"attempt": len(self.contexts)},
            cost_usd=0.01,
        )


class TestGraphRunner:
    def test_runs_nodes_in_dependency_order_and_stores_handoff_artifacts(
        self, tmp_path: Path
    ) -> None:
        graph = _graph(
            _node("planner", output=ArtifactKind.PLAN),
            _node(
                "implementer",
                depends_on=("planner",),
                required_artifacts=(ArtifactKind.PLAN,),
                output=ArtifactKind.DIFF,
            ),
        )
        store = ArtifactStore(tmp_path / "artifacts.db", blob_root=tmp_path / "blobs")
        executor = _RecordingExecutor(["plan text", "diff text"])
        result = GraphRunner(executor=executor, artifact_store=store).run(graph)

        assert result.graph.status is GraphStatus.COMPLETED
        assert [run.node_id for run in result.node_runs] == ["planner", "implementer"]
        assert all(run.status is NodeRunStatus.COMPLETED for run in result.node_runs)
        assert store.read_text(result.node_runs[0].artifact_ids[0]) == "plan text"
        assert "plan text" in executor.contexts[1].dependency_artifact_text

    def test_retries_empty_output_then_completes(self, tmp_path: Path) -> None:
        graph = _graph(_node("planner", output=ArtifactKind.PLAN))
        store = ArtifactStore(tmp_path / "artifacts.db", blob_root=tmp_path / "blobs")
        executor = _RecordingExecutor(["", "plan text"])
        result = GraphRunner(executor=executor, artifact_store=store).run(graph)

        assert result.graph.status is GraphStatus.COMPLETED
        assert result.node_runs[0].attempts == 2
        assert store.read_text(result.node_runs[0].artifact_ids[0]) == "plan text"

    def test_blocks_graph_after_retry_exhaustion(self, tmp_path: Path) -> None:
        graph = _graph(_node("planner", output=ArtifactKind.PLAN))
        store = ArtifactStore(tmp_path / "artifacts.db", blob_root=tmp_path / "blobs")
        executor = _RecordingExecutor(["", ""])
        result = GraphRunner(executor=executor, artifact_store=store).run(graph)

        assert result.graph.status is GraphStatus.BLOCKED
        assert result.node_runs[0].status is NodeRunStatus.FAILED
        assert result.node_runs[0].attempts == 2
        assert "empty artifact" in (result.node_runs[0].error or "")

    def test_blocks_when_required_artifact_is_missing(self, tmp_path: Path) -> None:
        graph = _graph(
            _node(
                "qa",
                required_artifacts=(ArtifactKind.DIFF,),
                output=ArtifactKind.TEST_RESULT,
            )
        )
        store = ArtifactStore(tmp_path / "artifacts.db", blob_root=tmp_path / "blobs")
        executor = _RecordingExecutor(["should not run"])
        result = GraphRunner(executor=executor, artifact_store=store).run(graph)

        assert result.graph.status is GraphStatus.BLOCKED
        assert result.node_runs[0].status is NodeRunStatus.BLOCKED
        assert "missing required artifacts" in (result.node_runs[0].error or "")
        assert executor.contexts == []
