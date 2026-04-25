"""Append-only audit logger with SHA-256 hash chaining for tamper detection.

Each log entry is a JSONL line written atomically. Entries are chained:
``entry["prev_hash"]`` is the SHA-256 of the previous line's raw bytes, forming
a chain that makes silent insertion or deletion detectable.

Usage::

    logger = AuditLogger(Path("/var/log/maxwell/audit.jsonl"))
    logger.log_api_call(method="POST", path="/api/v1/tasks", status=202,
                        user=None, request_id="abc")

The ``AuditLogger`` is intentionally lightweight — no threads, no queues.
Callers serialise access via the surrounding async event loop.  The file is
opened in append mode on every write to be safe under SIGKILL.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "AuditEntry",
    "AuditLogger",
    "AuditViolationError",
    "verify_chain",
]

_GENESIS_HASH = "0" * 64  # prev_hash sentinel for the first entry

# Keys whose values must never appear verbatim in the audit log (#234).
_SENSITIVE_KEYS = frozenset({"authorization", "x-api-key", "api_key", "token", "password"})


def _redact_details(details: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *details* with sensitive values redacted recursively."""

    def _redact_value(value: Any, *, key: str | None = None) -> Any:
        if key is not None and key.lower() in _SENSITIVE_KEYS:
            return "***"
        if isinstance(value, dict):
            return {
                nested_key: _redact_value(nested_value, key=nested_key)
                for nested_key, nested_value in value.items()
            }
        if isinstance(value, list):
            return [_redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(_redact_value(item) for item in value)
        if isinstance(value, str) and value.lower().startswith("bearer "):
            # Catch inadvertent bearer token values regardless of key name.
            return "Bearer ***"
        return value

    return {key: _redact_value(value, key=key) for key, value in details.items()}


def _rechain(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild ``entry_hash`` and ``prev_hash`` for every entry after rotation.

    After old entries are pruned the first surviving entry still carries a
    ``prev_hash`` that points to a deleted record.  This helper walks the
    kept entries in order, resetting ``prev_hash`` to the previous entry's
    recomputed hash (or ``_GENESIS_HASH`` for the new first entry) and
    recomputing ``entry_hash`` from scratch so the chain is self-consistent.
    """
    prev = _GENESIS_HASH
    result: list[dict[str, Any]] = []
    for entry in entries:
        e = dict(entry)
        e["prev_hash"] = prev
        payload = json.dumps({k: v for k, v in e.items() if k != "entry_hash"}, sort_keys=True)
        e["entry_hash"] = hashlib.sha256(payload.encode()).hexdigest()
        prev = e["entry_hash"]
        result.append(e)
    return result


class AuditViolationError(RuntimeError):
    """Raised by ``verify_chain`` when the hash chain is broken."""


@dataclass
class AuditEntry:
    """A single audit log record (written as one JSON line)."""

    timestamp: str
    event_type: str
    method: str | None
    path: str | None
    status: int | None
    user: str | None
    request_id: str | None
    details: dict[str, Any]
    prev_hash: str
    entry_hash: str = field(default="", init=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "user": self.user,
            "request_id": self.request_id,
            "details": self.details,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


class AuditLogger:
    """Write-once, hash-chained audit log.

    Parameters
    ----------
    path:
        Destination JSONL file.  Parent directories are created on first write.
    retention_days:
        If >0, ``rotate()`` removes lines older than this many days (call it
        from a scheduler; not automatic so tests stay deterministic).
    """

    def __init__(self, path: Path, *, retention_days: int = 0) -> None:
        self._path = path
        self._retention_days = retention_days
        self._last_hash: str | None = None  # cached tail hash; None = unread

    # ── public API ──────────────────────────────────────────────────────────

    def log_api_call(
        self,
        *,
        method: str,
        path: str,
        status: int,
        user: str | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        return self._append(
            event_type="api_call",
            method=method,
            path=path,
            status=status,
            user=user,
            request_id=request_id,
            details=details or {},
        )

    def log_auth_decision(
        self,
        *,
        subject: str | None,
        role: str,
        endpoint: str,
        outcome: str,
    ) -> AuditEntry:
        return self._append(
            event_type="auth_decision",
            method=None,
            path=endpoint,
            status=None,
            user=subject,
            request_id=None,
            details={"role": role, "outcome": outcome},
        )

    def log_agent_operation(
        self,
        *,
        operation: str,
        task_id: str | None = None,
        repo: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        return self._append(
            event_type="agent_operation",
            method=None,
            path=None,
            status=None,
            user=None,
            request_id=None,
            details={
                "operation": operation,
                "task_id": task_id,
                "repo": repo,
                **(details or {}),
            },
        )

    def log_config_change(
        self,
        *,
        key: str,
        user: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        return self._append(
            event_type="config_change",
            method=None,
            path=None,
            status=None,
            user=user,
            request_id=None,
            details={"key": key, **(details or {})},
        )

    def entries(self, *, limit: int = 1000, offset: int = 0) -> list[dict[str, Any]]:
        """Return up to *limit* entries starting at *offset* (oldest first)."""
        if not self._path.is_file():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        slice_ = lines[offset : offset + limit]
        result = []
        for line in slice_:
            line = line.strip()
            if line:
                with contextlib.suppress(json.JSONDecodeError):
                    result.append(json.loads(line))
        return result

    def rotate(self) -> int:
        """Delete entries older than ``retention_days``.  Returns lines removed."""
        if self._retention_days <= 0 or not self._path.is_file():
            return 0
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)

        # Count how many entries will be pruned before logging the rotation event.
        lines_before = self._path.read_text(encoding="utf-8").splitlines()
        removed = 0
        for line in lines_before:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = datetime.fromisoformat(entry.get("timestamp", ""))
                if ts < cutoff:
                    removed += 1
            except (json.JSONDecodeError, ValueError):
                pass

        if not removed:
            return 0

        # Log the rotation event FIRST so it is part of the kept chain.
        self.log_agent_operation(
            operation="log_rotation",
            details={"removed": removed, "cutoff": cutoff.isoformat()},
        )

        # Re-read the file (now includes the rotation entry just appended).
        lines = self._path.read_text(encoding="utf-8").splitlines()
        kept_dicts: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts_str = entry.get("timestamp", "")
                # Keep the rotation entry (agent_operation) and all recent entries.
                # Malformed / unparseable timestamps are kept as well.
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts >= cutoff or entry.get("details", {}).get("operation") == "log_rotation":
                        kept_dicts.append(entry)
                except ValueError:
                    kept_dicts.append(entry)
            except json.JSONDecodeError:
                pass  # drop malformed lines during rotation; they would break the chain

        # Rebuild the hash chain so verify_chain() continues to pass.
        rechained = _rechain(kept_dicts)
        kept_lines = [json.dumps(e, separators=(",", ":")) for e in rechained]
        self._write_lines(kept_lines)
        # Update the cached tail hash to the last rechained entry.
        self._last_hash = rechained[-1]["entry_hash"] if rechained else None
        return removed

    # ── internals ───────────────────────────────────────────────────────────

    def _tail_hash(self) -> str:
        """Read the hash of the last line without loading the whole file."""
        if self._last_hash is not None:
            return self._last_hash
        if not self._path.is_file():
            return _GENESIS_HASH
        with self._path.open("rb") as fh:
            # Walk backwards to find the last non-empty line.
            fh.seek(0, 2)
            size = fh.tell()
            if size == 0:
                return _GENESIS_HASH
            pos = size - 1
            buf = b""
            while pos >= 0:
                fh.seek(pos)
                ch = fh.read(1)
                if ch == b"\n" and buf.strip():
                    break
                buf = ch + buf
                pos -= 1
            line = buf.strip()
        if not line:
            return _GENESIS_HASH
        try:
            obj: dict[str, Any] = json.loads(line)
            result: str = obj.get("entry_hash", _GENESIS_HASH)
            return result
        except json.JSONDecodeError:
            return _GENESIS_HASH

    def _append(
        self,
        *,
        event_type: str,
        method: str | None,
        path: str | None,
        status: int | None,
        user: str | None,
        request_id: str | None,
        details: dict[str, Any],
    ) -> AuditEntry:
        prev = self._tail_hash()
        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=event_type,
            method=method,
            path=path,
            status=status,
            user=user,
            request_id=request_id,
            details=_redact_details(details),
            prev_hash=prev,
        )
        d = entry.as_dict()
        # Hash everything except entry_hash itself.
        payload = json.dumps({k: v for k, v in d.items() if k != "entry_hash"}, sort_keys=True)
        entry.entry_hash = hashlib.sha256(payload.encode()).hexdigest()
        d["entry_hash"] = entry.entry_hash

        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(d, separators=(",", ":")) + "\n"
        with self._path.open("ab") as fh:
            fh.write(line.encode())
            fh.flush()
            os.fsync(fh.fileno())

        self._last_hash = entry.entry_hash
        return entry

    def _write_lines(self, lines: list[str]) -> None:
        """Atomically replace the log file (used by rotate())."""
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            os.unlink(tmp)
            raise


# ── standalone verifier ─────────────────────────────────────────────────────


def verify_chain(path: Path) -> list[dict[str, Any]]:
    """Verify every entry's hash and the chain links.

    Returns a list of violation dicts (empty = clean).  Each violation has:
    ``{"line": N, "error": "...", "entry": {...}}``.
    """
    if not path.is_file():
        return []
    violations: list[dict[str, Any]] = []
    prev_hash = _GENESIS_HASH
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                violations.append(
                    {"line": lineno, "error": f"JSON parse error: {exc}", "entry": raw}
                )
                continue
            stored_hash = obj.get("entry_hash", "")
            stored_prev = obj.get("prev_hash", "")
            # Re-derive the expected hash.
            payload = json.dumps(
                {k: v for k, v in obj.items() if k != "entry_hash"}, sort_keys=True
            )
            expected_hash = hashlib.sha256(payload.encode()).hexdigest()
            if stored_hash != expected_hash:
                violations.append({"line": lineno, "error": "entry_hash mismatch", "entry": obj})
            if stored_prev != prev_hash:
                violations.append(
                    {
                        "line": lineno,
                        "error": f"chain broken: expected prev_hash={prev_hash!r}, got {stored_prev!r}",
                        "entry": obj,
                    }
                )
            prev_hash = stored_hash or expected_hash
    return violations
