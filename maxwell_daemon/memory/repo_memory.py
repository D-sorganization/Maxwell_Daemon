"""Repo-carried memory records and JSONL storage.

This module intentionally stays file-backed and deterministic. It provides the
reviewable memory substrate under ``.maxwell/memory`` without coupling storage
to context selection or delegate execution.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast

from maxwell_daemon.contracts import require

__all__ = [
    "MemoryEntry",
    "MemoryProposal",
    "MemorySnapshot",
    "RepoMemoryStore",
    "redact_secret_looking_values",
    "reject_secret_looking_values",
    "select_memory_snapshot",
]

MemoryScope = (
    str  # e.g., 'personal', 'repo:<name>', 'workspace:<id>', 'conversation:<id>', 'ephemeral'
)
MemoryKind = Literal["semantic", "episodic", "procedural", "policy"]
ProposalStatus = Literal["pending", "accepted", "rejected", "superseded"]


def is_valid_scope(scope: str) -> bool:
    if scope in {"personal", "ephemeral", "user-preference", "issue", "gate", "tool", "repo"}:
        # keep legacy scopes for backward compatibility
        return True
    for prefix in ("repo:", "workspace:", "conversation:"):
        if scope.startswith(prefix) and len(scope) > len(prefix):
            return True
    return False


_KINDS: set[str] = {"semantic", "episodic", "procedural", "policy"}
_STATUSES: set[str] = {"pending", "accepted", "rejected", "superseded"}
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY)[A-Za-z0-9_]*\s*=", re.I),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    id: str
    scope: str
    repo_id: str
    work_item_id: str | None
    kind: str
    body: str
    source: str
    confidence: float
    created_at: datetime = field(default_factory=_utcnow)
    expires_at: datetime | None = None
    supersedes: tuple[str, ...] = ()
    allow_secrets: bool = False
    retention_days: int | None = None
    provenance: str | None = None

    def __post_init__(self) -> None:
        require(bool(self.id.strip()), "MemoryEntry: id must be non-empty")
        require(is_valid_scope(self.scope), f"MemoryEntry: unsupported scope {self.scope!r}")
        require(bool(self.repo_id.strip()), "MemoryEntry: repo_id must be non-empty")
        require(self.kind in _KINDS, f"MemoryEntry: unsupported kind {self.kind!r}")
        if not self.allow_secrets:
            object.__setattr__(self, "body", redact_secret_looking_values(self.body))
            object.__setattr__(self, "source", redact_secret_looking_values(self.source))

        require(bool(self.body.strip()), "MemoryEntry: body must be non-empty")
        require(bool(self.source.strip()), "MemoryEntry: source must be non-empty")
        require(0.0 <= self.confidence <= 1.0, "MemoryEntry: confidence must be between 0 and 1")
        if self.expires_at is None:
            if self.scope == "ephemeral":
                object.__setattr__(self, "expires_at", self.created_at + timedelta(hours=24))
            elif self.scope.startswith("conversation:"):
                object.__setattr__(self, "expires_at", self.created_at + timedelta(days=30))
            elif self.retention_days is not None:
                object.__setattr__(
                    self, "expires_at", self.created_at + timedelta(days=self.retention_days)
                )
        if self.scope == "user-preference":
            require(
                self.source.startswith("user:") or "user" in self.source.lower(),
                "MemoryEntry: user-preference memory requires user-origin evidence",
            )
        if self.expires_at is not None:
            require(
                self.expires_at > self.created_at,
                "MemoryEntry: expires_at must be after created_at",
            )
        require(self.id not in self.supersedes, "MemoryEntry: entry cannot supersede itself")

    def to_json_dict(self) -> dict[str, object]:
        if not self.allow_secrets:
            reject_secret_looking_values(
                {"body": self.body, "source": self.source, "supersedes": list(self.supersedes)}
            )
        return {
            "body": self.body,
            "confidence": self.confidence,
            "created_at": _format_datetime(self.created_at),
            "expires_at": _format_datetime(self.expires_at) if self.expires_at else None,
            "id": self.id,
            "kind": self.kind,
            "repo_id": self.repo_id,
            "scope": self.scope,
            "source": self.source,
            "supersedes": list(self.supersedes),
            "work_item_id": self.work_item_id,
            "allow_secrets": self.allow_secrets,
            "retention_days": self.retention_days,
            "provenance": self.provenance,
        }

    @classmethod
    def from_json_dict(cls, payload: dict[str, object]) -> MemoryEntry:
        confidence = payload.get("confidence")
        require(
            isinstance(confidence, (float, int, str)),
            "confidence must be a float-compatible value",
        )
        assert isinstance(confidence, (float, int, str))
        return cls(
            id=_required_str(payload, "id"),
            scope=_required_str(payload, "scope"),
            repo_id=_required_str(payload, "repo_id"),
            work_item_id=_optional_str(payload, "work_item_id"),
            kind=_required_str(payload, "kind"),
            body=_required_str(payload, "body"),
            source=_required_str(payload, "source"),
            confidence=float(confidence),
            created_at=_parse_datetime(_required_str(payload, "created_at")),
            expires_at=_parse_optional_datetime(payload.get("expires_at")),
            supersedes=tuple(_str_list(payload.get("supersedes", []), "supersedes")),
            allow_secrets=bool(payload.get("allow_secrets", False)),
            retention_days=int(payload["retention_days"])
            if payload.get("retention_days") is not None
            else None,
            provenance=_optional_str(payload, "provenance"),
        )


@dataclass(frozen=True, slots=True)
class MemoryProposal:
    id: str
    proposed_by: str
    reason: str
    evidence: tuple[str, ...]
    target_scope: str
    entry: MemoryEntry
    status: str = "pending"
    created_at: datetime = field(default_factory=_utcnow)
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_reason: str | None = None

    def __post_init__(self) -> None:
        require(bool(self.id.strip()), "MemoryProposal: id must be non-empty")
        require(bool(self.proposed_by.strip()), "MemoryProposal: proposed_by must be non-empty")
        require(bool(self.reason.strip()), "MemoryProposal: reason must be non-empty")
        require(bool(self.evidence), "MemoryProposal: evidence must be non-empty")
        require(
            is_valid_scope(self.target_scope),
            f"MemoryProposal: unsupported target_scope {self.target_scope!r}",
        )
        require(
            self.target_scope == self.entry.scope,
            "MemoryProposal: target_scope must match entry scope",
        )
        require(self.status in _STATUSES, f"MemoryProposal: unsupported status {self.status!r}")
        if self.status == "accepted":
            require(bool(self.reviewed_by), "MemoryProposal: accepted proposal requires reviewer")
        if self.status == "rejected":
            require(bool(self.reviewed_by), "MemoryProposal: rejected proposal requires reviewer")
        if self.status == "superseded":
            require(bool(self.reviewed_by), "MemoryProposal: superseded proposal requires reviewer")

    def reviewed(
        self,
        *,
        status: ProposalStatus,
        reviewer: str,
        reason: str | None = None,
    ) -> MemoryProposal:
        require(status != "pending", "MemoryProposal.reviewed: status must be terminal")
        require(bool(reviewer.strip()), "MemoryProposal.reviewed: reviewer must be non-empty")
        return MemoryProposal(
            id=self.id,
            proposed_by=self.proposed_by,
            reason=self.reason,
            evidence=self.evidence,
            target_scope=self.target_scope,
            entry=self.entry,
            status=status,
            created_at=self.created_at,
            reviewed_by=reviewer,
            reviewed_at=_utcnow(),
            review_reason=reason,
        )

    def to_json_dict(self) -> dict[str, object]:
        reject_secret_looking_values(
            {
                "reason": self.reason,
                "evidence": list(self.evidence),
                "entry": self.entry.to_json_dict(),
            }
        )
        return {
            "created_at": _format_datetime(self.created_at),
            "entry": self.entry.to_json_dict(),
            "evidence": list(self.evidence),
            "id": self.id,
            "proposed_by": self.proposed_by,
            "reason": self.reason,
            "review_reason": self.review_reason,
            "reviewed_at": _format_datetime(self.reviewed_at) if self.reviewed_at else None,
            "reviewed_by": self.reviewed_by,
            "status": self.status,
            "target_scope": self.target_scope,
        }

    @classmethod
    def from_json_dict(cls, payload: dict[str, object]) -> MemoryProposal:
        entry_payload = payload.get("entry")
        require(isinstance(entry_payload, dict), "MemoryProposal: entry payload must be an object")
        entry_payload = cast(dict[str, object], entry_payload)
        return cls(
            id=_required_str(payload, "id"),
            proposed_by=_required_str(payload, "proposed_by"),
            reason=_required_str(payload, "reason"),
            evidence=tuple(_str_list(payload.get("evidence", []), "evidence")),
            target_scope=_required_str(payload, "target_scope"),
            entry=MemoryEntry.from_json_dict(entry_payload),
            status=_required_str(payload, "status"),
            created_at=_parse_datetime(_required_str(payload, "created_at")),
            reviewed_by=_optional_str(payload, "reviewed_by"),
            reviewed_at=_parse_optional_datetime(payload.get("reviewed_at")),
            review_reason=_optional_str(payload, "review_reason"),
        )


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    repo_id: str
    entries: tuple[MemoryEntry, ...]
    token_budget: int
    selection_reasons: dict[str, str]

    def __post_init__(self) -> None:
        require(bool(self.repo_id.strip()), "MemorySnapshot: repo_id must be non-empty")
        require(self.token_budget >= 0, "MemorySnapshot: token_budget must be non-negative")
        require(
            set(self.selection_reasons) == {entry.id for entry in self.entries},
            "MemorySnapshot: selection reasons must cover every entry",
        )

    def render(self, *, max_chars: int = 4000) -> str:
        if not self.entries:
            return ""
        lines = [f"## Repo memory snapshot for {self.repo_id}"]
        for entry in self.entries:
            reason = self.selection_reasons[entry.id]
            lines.append(f"- {entry.scope}/{entry.kind} {entry.id} ({reason})")
            lines.append(f"  source: {entry.source}")
            lines.append(f"  confidence: {entry.confidence:.2f}")
            if entry.work_item_id is not None:
                lines.append(f"  work item: {entry.work_item_id}")
            for body_line in entry.body.strip().splitlines():
                lines.append(f"  {body_line}")
        rendered = "\n".join(lines)
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars] + "\n... (truncated)"
        return rendered


class RepoMemoryStore:
    """Append-only JSONL store rooted at ``<repo>/.maxwell/memory``."""

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = Path(repo_root)
        self._memory_dir = self._repo_root / ".maxwell" / "memory"
        self._entries_path = self._memory_dir / "repo.jsonl"
        self._proposals_path = self._memory_dir / "proposals.jsonl"

    @property
    def memory_dir(self) -> Path:
        return self._memory_dir

    def add_entry(self, entry: MemoryEntry) -> None:
        if not entry.allow_secrets:
            reject_secret_looking_values(entry.to_json_dict())
        existing = {item.id for item in self._load_entries()}
        require(
            entry.id not in existing, f"RepoMemoryStore.add_entry: duplicate entry id {entry.id!r}"
        )
        self._append_jsonl(self._entries_path, entry.to_json_dict())

    def list_entries(
        self,
        *,
        repo_id: str | None = None,
        include_superseded: bool = False,
        now: datetime | None = None,
    ) -> list[MemoryEntry]:
        entries = self._load_entries()
        if repo_id is not None:
            entries = [entry for entry in entries if entry.repo_id == repo_id]
        if include_superseded:
            return entries

        reference_time = now or _utcnow()
        superseded_ids = {superseded for entry in entries for superseded in entry.supersedes}
        return [
            entry
            for entry in entries
            if entry.id not in superseded_ids
            and (entry.expires_at is None or entry.expires_at > reference_time)
        ]

    def export_jsonl(self, scope: str, out_path: Path) -> None:
        entries = [e for e in self.list_entries(include_superseded=True) if e.scope == scope]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8", newline="\n") as handle:
            for e in entries:
                handle.write(
                    f"{json.dumps(e.to_json_dict(), sort_keys=True, separators=(',', ':'))}\n"
                )

    def import_jsonl(
        self, in_path: Path, target_scope: str, *, allow_promotion: bool = False
    ) -> int:
        if not in_path.exists():
            return 0
        imported_count = 0
        existing_ids = {e.id for e in self._load_entries()}
        for payload in _read_jsonl(in_path):
            entry = MemoryEntry.from_json_dict(payload)
            if entry.id in existing_ids:
                continue
            if entry.scope == "personal" and not target_scope.startswith("personal"):
                require(
                    allow_promotion,
                    "Cannot promote personal memory to broader scope without explicit flag",
                )

            # Rewrite scope if it doesn't match? Or just enforce they match target_scope?
            # Issue says: "Import respects the scope; refuses to promote personal memory into repo:* without an explicit flag."
            # We'll rewrite the scope to target_scope
            if entry.scope != target_scope:
                object.__setattr__(entry, "scope", target_scope)

            self.add_entry(entry)
            imported_count += 1
        return imported_count

    def propose(self, proposal: MemoryProposal) -> MemoryProposal:
        require(proposal.status == "pending", "RepoMemoryStore.propose: proposal must be pending")
        reject_secret_looking_values(proposal.to_json_dict())
        latest = self._latest_proposals()
        require(
            proposal.id not in latest,
            f"RepoMemoryStore.propose: duplicate proposal id {proposal.id!r}",
        )
        self._append_jsonl(self._proposals_path, proposal.to_json_dict())
        return proposal

    def list_proposals(self) -> list[MemoryProposal]:
        return self._load_proposals()

    def latest_proposals(self) -> list[MemoryProposal]:
        return list(self._latest_proposals().values())

    def accept_proposal(
        self,
        proposal_id: str,
        *,
        reviewer: str,
        reason: str | None = None,
    ) -> MemoryProposal:
        proposal = self._pending_proposal(proposal_id)
        accepted = proposal.reviewed(status="accepted", reviewer=reviewer, reason=reason)
        self.add_entry(accepted.entry)
        self._append_jsonl(self._proposals_path, accepted.to_json_dict())
        return accepted

    def reject_proposal(
        self,
        proposal_id: str,
        *,
        reviewer: str,
        reason: str | None = None,
    ) -> MemoryProposal:
        rejected = self._pending_proposal(proposal_id).reviewed(
            status="rejected",
            reviewer=reviewer,
            reason=reason,
        )
        self._append_jsonl(self._proposals_path, rejected.to_json_dict())
        return rejected

    def supersede_proposal(
        self,
        proposal_id: str,
        *,
        reviewer: str,
        reason: str | None = None,
    ) -> MemoryProposal:
        superseded = self._pending_proposal(proposal_id).reviewed(
            status="superseded",
            reviewer=reviewer,
            reason=reason,
        )
        self._append_jsonl(self._proposals_path, superseded.to_json_dict())
        return superseded

    def load_snapshot(
        self,
        *,
        repo_id: str,
        work_item_id: str | None = None,
        max_items: int = 12,
        token_budget: int = 800,
        include_superseded: bool = False,
    ) -> MemorySnapshot:
        entries = self.list_entries(repo_id=repo_id, include_superseded=include_superseded)
        return select_memory_snapshot(
            entries,
            repo_id=repo_id,
            work_item_id=work_item_id,
            max_items=max_items,
            token_budget=token_budget,
        )

    def render_snapshot(
        self,
        *,
        repo_id: str,
        work_item_id: str | None = None,
        max_items: int = 12,
        token_budget: int = 800,
        include_superseded: bool = False,
        max_chars: int = 4000,
    ) -> str:
        snapshot = self.load_snapshot(
            repo_id=repo_id,
            work_item_id=work_item_id,
            max_items=max_items,
            token_budget=token_budget,
            include_superseded=include_superseded,
        )
        return snapshot.render(max_chars=max_chars)

    def find_conflicts(self, candidate: MemoryEntry) -> list[MemoryEntry]:
        reject_secret_looking_values(candidate.to_json_dict())
        conflicts: list[MemoryEntry] = []
        for entry in self.list_entries(repo_id=candidate.repo_id):
            if entry.id == candidate.id:
                continue
            if entry.id in candidate.supersedes or candidate.id in entry.supersedes:
                continue
            if (
                entry.scope == candidate.scope
                and entry.work_item_id == candidate.work_item_id
                and entry.kind == candidate.kind
                and _normalized_body(entry.body) != _normalized_body(candidate.body)
            ):
                conflicts.append(entry)
        return conflicts

    def _pending_proposal(self, proposal_id: str) -> MemoryProposal:
        require(bool(proposal_id.strip()), "RepoMemoryStore: proposal_id must be non-empty")
        latest = self._latest_proposals()
        require(proposal_id in latest, f"RepoMemoryStore: proposal {proposal_id!r} does not exist")
        proposal = latest[proposal_id]
        require(proposal.status == "pending", "RepoMemoryStore: proposal is not pending")
        return proposal

    def _latest_proposals(self) -> dict[str, MemoryProposal]:
        proposals: dict[str, MemoryProposal] = {}
        for proposal in self._load_proposals():
            proposals[proposal.id] = proposal
        return proposals

    def _load_entries(self) -> list[MemoryEntry]:
        return [MemoryEntry.from_json_dict(payload) for payload in _read_jsonl(self._entries_path)]

    def _load_proposals(self) -> list[MemoryProposal]:
        return [
            MemoryProposal.from_json_dict(payload) for payload in _read_jsonl(self._proposals_path)
        ]

    def _append_jsonl(self, path: Path, payload: dict[str, object]) -> None:
        reject_secret_looking_values(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{line}\n")


def select_memory_snapshot(
    entries: list[MemoryEntry],
    *,
    repo_id: str,
    work_item_id: str | None = None,
    max_items: int = 12,
    token_budget: int = 800,
) -> MemorySnapshot:
    require(bool(repo_id.strip()), "select_memory_snapshot: repo_id must be non-empty")
    require(max_items >= 0, "select_memory_snapshot: max_items must be non-negative")
    require(token_budget >= 0, "select_memory_snapshot: token_budget must be non-negative")

    selected: list[MemoryEntry] = []
    reasons: dict[str, str] = {}
    used_tokens = 0

    for entry in sorted(entries, key=_selection_key):
        if len(selected) >= max_items:
            break
        reason = _selection_reason(entry, repo_id=repo_id, work_item_id=work_item_id)
        if reason is None:
            continue
        estimate = _estimate_tokens(entry.body)
        if used_tokens + estimate > token_budget:
            continue
        selected.append(entry)
        reasons[entry.id] = reason
        used_tokens += estimate

    return MemorySnapshot(
        repo_id=repo_id,
        entries=tuple(selected),
        token_budget=token_budget,
        selection_reasons=reasons,
    )


def reject_secret_looking_values(payload: object) -> None:
    text = _stringify_payload(payload)
    require(not _has_secret_looking_value(text), "repo memory contains a secret-looking value")


def redact_secret_looking_values(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _selection_reason(
    entry: MemoryEntry,
    *,
    repo_id: str,
    work_item_id: str | None,
) -> str | None:
    if entry.repo_id != repo_id:
        return None
    if entry.scope == "issue":
        return "work item match" if entry.work_item_id == work_item_id else None
    return "repo match"


def _selection_key(entry: MemoryEntry) -> tuple[int, str, str]:
    scope_rank = {
        "repo": 0,
        "tool": 1,
        "gate": 2,
        "issue": 3,
        "user-preference": 4,
    }.get(entry.scope, 99)
    return (scope_rank, entry.created_at.isoformat(), entry.id)


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    payloads: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            require(
                isinstance(payload, dict),
                f"{path}:{line_number}: JSONL record must be an object",
            )
            payloads.append(payload)
    return payloads


def _required_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    require(isinstance(value, str), f"{key} must be a string")
    assert isinstance(value, str)
    require(bool(value.strip()), f"{key} must be non-empty")
    return value


def _optional_str(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    require(isinstance(value, str), f"{key} must be a string or null")
    assert isinstance(value, str)
    return value


def _str_list(value: object, key: str) -> list[str]:
    require(isinstance(value, list), f"{key} must be a list")
    assert isinstance(value, list)
    result: list[str] = []
    for item in value:
        require(isinstance(item, str), f"{key} items must be strings")
        result.append(item)
    return result


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    require(isinstance(value, str), "datetime value must be a string or null")
    assert isinstance(value, str)
    return _parse_datetime(value)


def _normalized_body(body: str) -> str:
    return " ".join(body.lower().split())


def _stringify_payload(payload: object) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, sort_keys=True, default=str)


def _has_secret_looking_value(text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in _SECRET_PATTERNS)
