"""Durable storage for task graph definitions and node run records."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from maxwell_daemon.director.task_graphs import GraphStatus, NodeRun, TaskGraph

__all__ = ["TaskGraphRecord", "TaskGraphStore"]


@dataclass(slots=True, frozen=True)
class TaskGraphRecord:
    """Stored task graph state plus the latest node run records."""

    graph: TaskGraph
    node_runs: tuple[NodeRun, ...] = ()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_graphs (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL,
    status TEXT NOT NULL,
    graph_json TEXT NOT NULL,
    node_runs_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_graphs_work_item
    ON task_graphs(work_item_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_task_graphs_status
    ON task_graphs(status, updated_at);
"""


class TaskGraphStore:
    """SQLite-backed task graph record store."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def save_graph(self, graph: TaskGraph) -> TaskGraphRecord:
        """Persist a graph definition without changing existing node runs."""
        existing = self.get(graph.id)
        node_runs = existing.node_runs if existing is not None else ()
        return self.save_record(TaskGraphRecord(graph=graph, node_runs=node_runs))

    def save_record(self, record: TaskGraphRecord) -> TaskGraphRecord:
        graph = record.graph
        row = (
            graph.id,
            graph.work_item_id,
            graph.status.value,
            graph.model_dump_json(),
            json.dumps(
                [run.model_dump(mode="json") for run in record.node_runs],
                separators=(",", ":"),
                sort_keys=True,
            ),
            graph.created_at.isoformat(),
            graph.updated_at.isoformat(),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_graphs (
                    id, work_item_id, status, graph_json, node_runs_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    work_item_id = excluded.work_item_id,
                    status = excluded.status,
                    graph_json = excluded.graph_json,
                    node_runs_json = excluded.node_runs_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                row,
            )
        loaded = self.get(graph.id)
        if loaded is None:
            raise RuntimeError(f"task graph {graph.id!r} was not persisted")
        return loaded

    def get(self, graph_id: str) -> TaskGraphRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_graphs WHERE id = ?", (graph_id,)
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_records(
        self,
        *,
        work_item_id: str | None = None,
        status: GraphStatus | None = None,
        limit: int = 100,
    ) -> list[TaskGraphRecord]:
        query = "SELECT * FROM task_graphs"
        clauses: list[str] = []
        args: list[object] = []
        if work_item_id is not None:
            clauses.append("work_item_id = ?")
            args.append(work_item_id)
        if status is not None:
            clauses.append("status = ?")
            args.append(status.value)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, id ASC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [_row_to_record(row) for row in rows]


def _row_to_record(row: sqlite3.Row) -> TaskGraphRecord:
    graph = TaskGraph.model_validate_json(row["graph_json"])
    node_runs = tuple(
        NodeRun.model_validate(item) for item in json.loads(row["node_runs_json"])
    )
    return TaskGraphRecord(graph=graph, node_runs=node_runs)
