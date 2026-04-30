"""Sequential task graph runner with typed artifact handoffs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from maxwell_daemon.core.artifacts import Artifact, ArtifactKind
from maxwell_daemon.director.task_graphs import (
    GraphNode,
    GraphStatus,
    NodeRun,
    NodeRunStatus,
    TaskGraph,
)

__all__ = [
    "GraphExecutionContext",
    "GraphExecutionResult",
    "GraphNodeExecutor",
    "GraphNodeOutput",
    "GraphRunner",
    "GraphRunnerError",
]


class GraphRunnerError(RuntimeError):
    """Raised when a task graph cannot run because its dependencies are invalid."""


@dataclass(slots=True, frozen=True)
class GraphExecutionContext:
    graph: TaskGraph
    node: GraphNode
    dependency_artifact_ids: tuple[str, ...]
    dependency_artifact_text: str


@dataclass(slots=True, frozen=True)
class GraphNodeOutput:
    text: str
    media_type: str = "text/markdown"
    metadata: dict[str, object] | None = None
    cost_usd: float = 0.0


@dataclass(slots=True, frozen=True)
class GraphExecutionResult:
    graph: TaskGraph
    node_runs: tuple[NodeRun, ...]


class GraphNodeExecutor(Protocol):
    def execute(self, context: GraphExecutionContext) -> GraphNodeOutput: ...


class ArtifactStoreProtocol(Protocol):
    def get(self, artifact_id: str) -> Artifact | None: ...

    def put_text(
        self,
        *,
        kind: ArtifactKind,
        name: str,
        text: str,
        task_id: str | None = None,
        work_item_id: str | None = None,
        media_type: str = "text/plain; charset=utf-8",
        metadata: dict[str, object] | None = None,
    ) -> Artifact: ...

    def read_text(self, artifact_id: str) -> str: ...


class GraphRunner:
    """Run a validated task graph in dependency order.

    The runner intentionally depends on small executor/artifact-store protocols
    so the orchestration model can be tested without daemon internals.
    """

    def __init__(
        self,
        *,
        executor: GraphNodeExecutor,
        artifact_store: ArtifactStoreProtocol,
    ) -> None:
        self._executor = executor
        self._artifact_store = artifact_store

    def run(self, graph: TaskGraph) -> GraphExecutionResult:
        graph = graph.model_copy(update={"status": GraphStatus.RUNNING, "updated_at": _now()})
        node_runs: list[NodeRun] = []
        artifacts_by_node: dict[str, tuple[Artifact, ...]] = {}
        artifacts_by_kind: dict[ArtifactKind, list[Artifact]] = {}

        for node in graph.nodes_in_dependency_order():
            missing_deps = [dep for dep in node.depends_on if dep not in artifacts_by_node]
            if missing_deps:
                run = _blocked_run(
                    graph_id=graph.id,
                    node_id=node.id,
                    error=f"missing completed dependencies: {', '.join(missing_deps)}",
                )
                node_runs.append(run)
                graph = graph.model_copy(
                    update={
                        "status": GraphStatus.BLOCKED,
                        "updated_at": run.finished_at,
                    }
                )
                return GraphExecutionResult(graph=graph, node_runs=tuple(node_runs))

            missing_artifacts = [
                kind.value for kind in node.required_artifacts if kind not in artifacts_by_kind
            ]
            if missing_artifacts:
                run = _blocked_run(
                    graph_id=graph.id,
                    node_id=node.id,
                    error=f"missing required artifacts: {', '.join(missing_artifacts)}",
                )
                node_runs.append(run)
                graph = graph.model_copy(
                    update={
                        "status": GraphStatus.BLOCKED,
                        "updated_at": run.finished_at,
                    }
                )
                return GraphExecutionResult(graph=graph, node_runs=tuple(node_runs))

            run = self._run_node(graph, node, artifacts_by_node)
            node_runs.append(run)
            if run.status is not NodeRunStatus.COMPLETED:
                graph = graph.model_copy(
                    update={
                        "status": GraphStatus.BLOCKED,
                        "updated_at": run.finished_at,
                    }
                )
                return GraphExecutionResult(graph=graph, node_runs=tuple(node_runs))

            node_artifacts = tuple(
                artifact
                for artifact_id in run.artifact_ids
                for artifact in (self._resolve_artifact(artifact_id),)
            )
            artifacts_by_node[node.id] = node_artifacts
            for artifact in node_artifacts:
                artifacts_by_kind.setdefault(artifact.kind, []).append(artifact)

        graph = graph.model_copy(update={"status": GraphStatus.COMPLETED, "updated_at": _now()})
        return GraphExecutionResult(graph=graph, node_runs=tuple(node_runs))

    def _run_node(
        self,
        graph: TaskGraph,
        node: GraphNode,
        artifacts_by_node: dict[str, tuple[Artifact, ...]],
    ) -> NodeRun:
        started_at = _now()
        attempts = 0
        last_error = ""
        dependency_ids = tuple(
            artifact.id for dep in node.depends_on for artifact in artifacts_by_node.get(dep, ())
        )
        dependency_text = self._render_dependency_artifacts(dependency_ids)
        context = GraphExecutionContext(
            graph=graph,
            node=node,
            dependency_artifact_ids=dependency_ids,
            dependency_artifact_text=dependency_text,
        )

        for attempt in range(node.max_retries + 1):
            attempts = attempt + 1
            try:
                output = self._executor.execute(context)
                if not output.text.strip():
                    raise GraphRunnerError("node produced an empty artifact")
                artifact = self._artifact_store.put_text(
                    kind=node.output_artifact_kind,
                    name=f"{node.id} {node.output_artifact_kind.value}",
                    text=output.text,
                    work_item_id=graph.work_item_id,
                    media_type=output.media_type,
                    metadata={
                        "graph_id": graph.id,
                        "node_id": node.id,
                        "role": node.role.value,
                        **(output.metadata or {}),
                    },
                )
                finished_at = _now()
                return NodeRun(
                    graph_id=graph.id,
                    node_id=node.id,
                    status=NodeRunStatus.COMPLETED,
                    artifact_ids=(artifact.id,),
                    started_at=started_at,
                    finished_at=finished_at,
                    cost_usd=output.cost_usd,
                    attempts=attempts,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc) or repr(exc)

        finished_at = _now()
        return NodeRun(
            graph_id=graph.id,
            node_id=node.id,
            status=NodeRunStatus.FAILED,
            started_at=started_at,
            finished_at=finished_at,
            attempts=attempts,
            error=last_error,
        )

    def _render_dependency_artifacts(self, artifact_ids: tuple[str, ...]) -> str:
        parts: list[str] = []
        for artifact_id in artifact_ids:
            parts.append(f"## Artifact {artifact_id}\n")
            parts.append(self._artifact_store.read_text(artifact_id))
        return "\n".join(parts)

    def _resolve_artifact(self, artifact_id: str) -> Artifact:
        artifact = self._artifact_store.get(artifact_id)
        if artifact is None:
            raise GraphRunnerError(f"artifact {artifact_id!r} disappeared during graph execution")
        return artifact


def _blocked_run(*, graph_id: str, node_id: str, error: str) -> NodeRun:
    timestamp = _now()
    return NodeRun(
        graph_id=graph_id,
        node_id=node_id,
        status=NodeRunStatus.BLOCKED,
        started_at=timestamp,
        finished_at=timestamp,
        error=error,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)
