"""Searchable history of past issue→PR outcomes.

SQLite FTS5 over (title, body, plan) gives us good-enough retrieval without
pulling in a vector DB. When we outgrow keyword match we can add embeddings
as a second index without changing the public shape.

Only ``merged`` episodes are surfaced by default — we want the agent to learn
from what worked, not what got abandoned. Override via ``include_failed``.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from maxwell_daemon.contracts import require

__all__ = ["Episode", "EpisodicStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    issue_number INTEGER,
    issue_title TEXT,
    issue_body TEXT,
    plan TEXT,
    applied_diff INTEGER NOT NULL DEFAULT 0,
    pr_url TEXT,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_repo ON episodes(repo);
CREATE INDEX IF NOT EXISTS idx_episodes_outcome ON episodes(outcome);

CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    issue_title, issue_body, plan, content='episodes', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, issue_title, issue_body, plan)
    VALUES (new.rowid, new.issue_title, new.issue_body, new.plan);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, issue_title, issue_body, plan)
    VALUES ('delete', old.rowid, old.issue_title, old.issue_body, old.plan);
END;
CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, issue_title, issue_body, plan)
    VALUES ('delete', old.rowid, old.issue_title, old.issue_body, old.plan);
    INSERT INTO episodes_fts(rowid, issue_title, issue_body, plan)
    VALUES (new.rowid, new.issue_title, new.issue_body, new.plan);
END;
"""


@dataclass(slots=True, frozen=True)
class Episode:
    id: str
    repo: str
    issue_number: int | None
    issue_title: str
    issue_body: str
    plan: str
    applied_diff: bool
    pr_url: str
    outcome: str  # merged | closed | failed


class EpisodicStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        finally:
            conn.close()

    def record(self, episode: Episode) -> None:
        require(bool(episode.id), "EpisodicStore.record: id must be non-empty")
        require(bool(episode.repo), "EpisodicStore.record: repo must be non-empty")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            # Upsert so re-running the same task doesn't leave stale rows.
            conn.execute(
                """
                INSERT INTO episodes (
                    id, repo, issue_number, issue_title, issue_body, plan,
                    applied_diff, pr_url, outcome, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    issue_title=excluded.issue_title,
                    issue_body=excluded.issue_body,
                    plan=excluded.plan,
                    applied_diff=excluded.applied_diff,
                    pr_url=excluded.pr_url,
                    outcome=excluded.outcome
                """,
                (
                    episode.id,
                    episode.repo,
                    episode.issue_number,
                    episode.issue_title,
                    episode.issue_body,
                    episode.plan,
                    1 if episode.applied_diff else 0,
                    episode.pr_url,
                    episode.outcome,
                    now,
                ),
            )

    def search(
        self,
        query: str,
        *,
        repo: str | None = None,
        limit: int = 5,
        include_failed: bool = False,
    ) -> list[Episode]:
        if not query.strip():
            return []
        fts_expr = _fts_escape(query)
        if not fts_expr:
            return []
        args: list[object] = [fts_expr]
        where: list[str] = []
        if repo is not None:
            where.append("e.repo = ?")
            args.append(repo)
        if not include_failed:
            where.append("e.outcome IN ('merged', 'completed')")
        args.append(limit)
        if len(where) == 2:
            sql = """
            SELECT e.* FROM episodes_fts
            JOIN episodes e ON e.rowid = episodes_fts.rowid
            WHERE episodes_fts MATCH ?
            AND e.repo = ?
            AND e.outcome IN ('merged', 'completed')
            ORDER BY bm25(episodes_fts)
            LIMIT ?
            """
        elif where == ["e.repo = ?"]:
            sql = """
            SELECT e.* FROM episodes_fts
            JOIN episodes e ON e.rowid = episodes_fts.rowid
            WHERE episodes_fts MATCH ?
            AND e.repo = ?
            ORDER BY bm25(episodes_fts)
            LIMIT ?
            """
        elif where == ["e.outcome IN ('merged', 'completed')"]:
            sql = """
            SELECT e.* FROM episodes_fts
            JOIN episodes e ON e.rowid = episodes_fts.rowid
            WHERE episodes_fts MATCH ?
            AND e.outcome IN ('merged', 'completed')
            ORDER BY bm25(episodes_fts)
            LIMIT ?
            """
        else:
            sql = """
            SELECT e.* FROM episodes_fts
            JOIN episodes e ON e.rowid = episodes_fts.rowid
            WHERE episodes_fts MATCH ?
            ORDER BY bm25(episodes_fts)
            LIMIT ?
            """
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_row_to_episode(r) for r in rows]

    def render_related(self, query: str, *, repo: str | None = None, limit: int = 3) -> str:
        hits = self.search(query, repo=repo, limit=limit)
        if not hits:
            return ""
        lines: list[str] = []
        for e in hits:
            lines.append(f"- #{e.issue_number} {e.issue_title} → {e.pr_url}")
            if e.plan:
                snippet = e.plan.strip().splitlines()[0][:120]
                lines.append(f"  plan: {snippet}")
        return "\n".join(lines)


def _row_to_episode(row: sqlite3.Row) -> Episode:
    return Episode(
        id=row["id"],
        repo=row["repo"],
        issue_number=row["issue_number"],
        issue_title=row["issue_title"] or "",
        issue_body=row["issue_body"] or "",
        plan=row["plan"] or "",
        applied_diff=bool(row["applied_diff"]),
        pr_url=row["pr_url"] or "",
        outcome=row["outcome"],
    )


_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
    }
)


def _fts_escape(query: str) -> str:
    """Quote each non-trivial term and OR them so free-form input matches any.

    Default FTS5 semantics are AND-between-terms, too strict for episode
    retrieval — issue titles never share all the same words. We OR so partial
    overlap still surfaces a relevant episode. Stop words are dropped so a
    query like "fix the parser" doesn't blow up into three AND-clauses.
    """
    terms = [
        t
        for t in (word.strip(".,:;!?").lower() for word in query.split())
        if t
        and t not in _STOP_WORDS
        and t.replace("-", "").replace("_", "").isalnum()
        and len(t) > 2
    ]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in terms)
