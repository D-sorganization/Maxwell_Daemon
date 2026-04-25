"""Task graph control-plane service."""

from __future__ import annotations

from maxwell_daemon.core.artifacts import ArtifactStore
from maxwell_daemon.core.work_items import WorkItem
from maxwell_daemon.director.task_graph_runner import GraphNodeExecutor, GraphRunner
from maxwell_daemon.director.task_graph_store import TaskGraphRecord, TaskGraphStore
from maxwell_daemon.director.task_graphs import (
    GraphStatus,
    TaskGraphTemplate,
    build_task_graph,
)

__all__ = ["TaskGraphExecutorUnavailableError", "TaskGraphService"]


class TaskGraphExecutorUnavailableError(RuntimeError):
    """Raised when a graph start is requested before an executor is configured."""


class TaskGraphService:
    """Create, inspect, and start task graphs through narrow dependencies."""

    def __init__(
        self,
        *,
        store: TaskGraphStore,
        artifact_store: ArtifactStore,
        executor: GraphNodeExecutor | None = None,
    ) -> None:
        self._store = store
        self._artifact_store = artifact_store
        self._executor = executor

    def set_executor(self, executor: GraphNodeExecutor | None) -> None:
        self._executor = executor

    def create_from_work_item(
        self,
        work_item: WorkItem,
        *,
        template: TaskGraphTemplate | None = None,
        graph_id: str | None = None,
        labels: tuple[str, ...] = (),
    ) -> TaskGraphRecord:
        graph = build_task_graph(
            work_item,
            template=template,
            graph_id=graph_id,
            labels=labels,
        )
        return self._store.save_graph(graph)

    def get(self, graph_id: str) -> TaskGraphRecord | None:
        return self._store.get(graph_id)

    def list_records(
        self,
        *,
        work_item_id: str | None = None,
        status: GraphStatus | None = None,
        limit: int = 100,
    ) -> list[TaskGraphRecord]:
        return self._store.list_records(
            work_item_id=work_item_id, status=status, limit=limit
        )

    def start(self, graph_id: str) -> TaskGraphRecord:
        record = self._store.get(graph_id)
        if record is None:
            raise KeyError(graph_id)
        if record.graph.status is GraphStatus.COMPLETED:
            raise ValueError(f"task graph {graph_id!r} is already completed")
        if record.graph.status is GraphStatus.RUNNING:
            raise ValueError(f"task graph {graph_id!r} is already running")
        if self._executor is None:
            raise TaskGraphExecutorUnavailableError(
                "task graph executor is not configured; backend-routed graph execution "
                "is a follow-up slice"
            )

        result = GraphRunner(
            executor=self._executor,
            artifact_store=self._artifact_store,
        ).run(record.graph)
        return self._store.save_record(
            TaskGraphRecord(graph=result.graph, node_runs=result.node_runs)
        )
