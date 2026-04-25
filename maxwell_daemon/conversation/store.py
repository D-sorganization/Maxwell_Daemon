"""Persistent multi-turn conversation store.

Persists conversation history (a sequence of :class:`~maxwell_daemon.backends.base.Message`
objects) across task runs so that the agent can maintain context between sessions.

Two backends are provided:

* :class:`JsonConversationStore` — human-readable JSON files, one per conversation.
  Good for development and debugging.
* :class:`SqliteConversationStore` — SQLite-backed store for production use.
  Supports concurrent readers and atomic writes.

Both implement :class:`ConversationStore`, a simple protocol:

    store.save(conversation_id, messages)
    messages = store.load(conversation_id)   # [] if not found
    store.delete(conversation_id)
    ids = store.list_ids()
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from maxwell_daemon.backends.base import Message, MessageRole

__all__ = [
    "ConversationStore",
    "JsonConversationStore",
    "SqliteConversationStore",
]

# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _message_to_dict(msg: Message) -> dict[str, Any]:
    d = asdict(msg)
    d["role"] = msg.role.value  # store the string value, not the enum
    return d


def _message_from_dict(d: dict[str, Any]) -> Message:
    role = MessageRole(d["role"])
    return Message(
        role=role,
        content=d.get("content", ""),
        name=d.get("name"),
        tool_call_id=d.get("tool_call_id"),
        metadata=d.get("metadata", {}),
    )


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class ConversationStore(ABC):
    """Minimal interface for persisting multi-turn conversation histories."""

    @abstractmethod
    def save(self, conversation_id: str, messages: list[Message]) -> None:
        """Persist ``messages`` under ``conversation_id``, overwriting any prior history."""

    @abstractmethod
    def load(self, conversation_id: str) -> list[Message]:
        """Return the message history for ``conversation_id``, or ``[]`` if absent."""

    @abstractmethod
    def delete(self, conversation_id: str) -> None:
        """Remove the conversation. A no-op if it does not exist."""

    @abstractmethod
    def list_ids(self) -> list[str]:
        """Return all known conversation IDs in insertion order."""

    def append(self, conversation_id: str, message: Message) -> None:
        """Append a single ``message`` to an existing or new conversation."""
        messages = self.load(conversation_id)
        messages.append(message)
        self.save(conversation_id, messages)


# ---------------------------------------------------------------------------
# JSON file backend
# ---------------------------------------------------------------------------


class JsonConversationStore(ConversationStore):
    """One JSON file per conversation, stored under ``directory``.

    Files are named ``<conversation_id>.json``. The format is a plain JSON
    array of message dicts, making them easy to inspect and edit.
    """

    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory).expanduser()
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, conversation_id: str) -> Path:
        # Guard against path traversal.
        if "/" in conversation_id or "\\" in conversation_id or conversation_id.startswith("."):
            raise ValueError(f"Invalid conversation_id: {conversation_id!r}")
        return self._dir / f"{conversation_id}.json"

    def save(self, conversation_id: str, messages: list[Message]) -> None:
        path = self._path(conversation_id)
        payload = [_message_to_dict(m) for m in messages]
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def load(self, conversation_id: str) -> list[Message]:
        path = self._path(conversation_id)
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return [_message_from_dict(d) for d in raw]
        except (json.JSONDecodeError, KeyError, ValueError):
            return []

    def delete(self, conversation_id: str) -> None:
        path = self._path(conversation_id)
        path.unlink(missing_ok=True)

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self._dir.glob("*.json"))


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


class SqliteConversationStore(ConversationStore):
    """SQLite-backed conversation store.

    All messages for a conversation are stored as a single JSON blob in one
    row, which keeps schema simple and queries fast for the typical access
    pattern (load all messages for one conversation).

    Thread-safety: SQLite connections are created per-call so this is safe
    to use from multiple threads (and async tasks) simultaneously.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self._path,
            isolation_level=None,
            check_same_thread=False,
            timeout=10.0,
        )
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    messages        TEXT NOT NULL DEFAULT '[]',
                    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )

    def save(self, conversation_id: str, messages: list[Message]) -> None:
        payload = json.dumps([_message_to_dict(m) for m in messages], ensure_ascii=False)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO conversations (conversation_id, messages, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(conversation_id) DO UPDATE SET
                    messages   = excluded.messages,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, payload),
            )

    def load(self, conversation_id: str) -> list[Message]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT messages FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return []
        try:
            raw = json.loads(row["messages"])
            return [_message_from_dict(d) for d in raw]
        except (json.JSONDecodeError, KeyError, ValueError):
            return []

    def delete(self, conversation_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            )

    def list_ids(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT conversation_id FROM conversations ORDER BY created_at"
            ).fetchall()
        return [row["conversation_id"] for row in rows]
