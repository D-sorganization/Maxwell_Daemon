"""Repo-carried memory models and JSONL storage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from maxwell_daemon.contracts import PreconditionError
from maxwell_daemon.memory.repo_memory import (
    MemoryEntry,
    MemoryProposal,
    RepoMemoryStore,
    redact_secret_looking_values,
    select_memory_snapshot,
)

_CREATED_AT = datetime(2026, 4, 22, 12, tzinfo=timezone.utc)


def _entry(
    entry_id: str,
    *,
    repo_id: str = "D-sorganization/Maxwell-Daemon",
    scope: str = "repo",
    work_item_id: str | None = None,
    kind: str = "semantic",
    body: str = "Run unit tests with pytest tests/unit.",
    source: str = "issue-397",
    confidence: float = 0.8,
    supersedes: tuple[str, ...] = (),
    created_at: datetime = _CREATED_AT,
    expires_at: datetime | None = None,
) -> MemoryEntry:
    return MemoryEntry(
        id=entry_id,
        scope=scope,
        repo_id=repo_id,
        work_item_id=work_item_id,
        kind=kind,
        body=body,
        source=source,
        confidence=confidence,
        created_at=created_at,
        expires_at=expires_at,
        supersedes=supersedes,
    )


def _proposal(proposal_id: str, entry: MemoryEntry) -> MemoryProposal:
    return MemoryProposal(
        id=proposal_id,
        proposed_by="delegate-1",
        reason="The command was validated in CI triage.",
        evidence=("pytest output showed this is the supported unit-test path.",),
        target_scope=entry.scope,
        entry=entry,
    )


def test_entry_contracts_require_stable_identity_source_and_valid_confidence() -> None:
    with pytest.raises(PreconditionError, match="id must be non-empty"):
        _entry("")

    with pytest.raises(PreconditionError, match="source must be non-empty"):
        _entry("m1", source="")

    with pytest.raises(PreconditionError, match="confidence"):
        _entry("m1", confidence=1.1)


def test_user_preference_requires_user_origin_evidence() -> None:
    with pytest.raises(PreconditionError, match="user-origin"):
        _entry("pref-1", scope="user-preference", source="delegate-1")

    entry = _entry(
        "pref-1",
        scope="user-preference",
        source="user:issue-comment",
        body="Prefer small reviewable PRs.",
    )

    assert entry.scope == "user-preference"


def test_proposals_stay_pending_until_reviewed(tmp_path: Path) -> None:
    store = RepoMemoryStore(tmp_path)
    proposal = store.propose(_proposal("p1", _entry("m1")))

    assert proposal.status == "pending"
    assert store.list_entries(repo_id="D-sorganization/Maxwell-Daemon") == []

    with pytest.raises(PreconditionError, match="reviewer"):
        store.accept_proposal("p1", reviewer="")

    accepted = store.accept_proposal("p1", reviewer="maintainer")

    assert accepted.status == "accepted"
    assert store.list_entries(repo_id="D-sorganization/Maxwell-Daemon") == [_entry("m1")]


def test_rejected_and_superseded_proposals_stay_out_of_accepted_memory(
    tmp_path: Path,
) -> None:
    store = RepoMemoryStore(tmp_path)
    store.propose(_proposal("p-reject", _entry("reject-me")))
    store.propose(_proposal("p-supersede", _entry("supersede-me")))

    rejected = store.reject_proposal("p-reject", reviewer="critic", reason="contradicted")
    superseded = store.supersede_proposal(
        "p-supersede", reviewer="critic", reason="replaced by newer proposal"
    )

    assert rejected.status == "rejected"
    assert superseded.status == "superseded"
    assert {proposal.id: proposal.status for proposal in store.latest_proposals()} == {
        "p-reject": "rejected",
        "p-supersede": "superseded",
    }
    assert store.list_entries(repo_id="D-sorganization/Maxwell-Daemon") == []


def test_duplicate_proposal_ids_are_rejected(tmp_path: Path) -> None:
    store = RepoMemoryStore(tmp_path)
    store.propose(_proposal("p1", _entry("m1")))

    with pytest.raises(PreconditionError, match="duplicate proposal id"):
        store.propose(_proposal("p1", _entry("m2")))


def test_redacts_secret_looking_values_before_writing(tmp_path: Path) -> None:
    store = RepoMemoryStore(tmp_path)
    secret = "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456"

    # It should automatically redact the secret
    proposal = store.propose(_proposal("p-secret", _entry("m-secret", body=secret)))

    assert "[REDACTED]" in proposal.entry.body
    assert "sk-proj-" not in proposal.entry.body


def test_superseded_entries_remain_inspectable_but_inactive(tmp_path: Path) -> None:
    store = RepoMemoryStore(tmp_path)
    store.add_entry(_entry("old", body="Use pytest."))
    store.add_entry(_entry("new", body="Use pytest tests/unit.", supersedes=("old",)))

    all_entries = store.list_entries(
        repo_id="D-sorganization/Maxwell-Daemon", include_superseded=True
    )
    active_entries = store.list_entries(repo_id="D-sorganization/Maxwell-Daemon")

    assert [entry.id for entry in all_entries] == ["old", "new"]
    assert [entry.id for entry in active_entries] == ["new"]


def test_expired_entries_are_filtered_from_active_memory(tmp_path: Path) -> None:
    store = RepoMemoryStore(tmp_path)
    expires_at = _CREATED_AT + timedelta(hours=1)
    store.add_entry(_entry("expired", body="Old fact.", expires_at=expires_at))
    store.add_entry(_entry("active", body="Current fact."))

    active_entries = store.list_entries(
        repo_id="D-sorganization/Maxwell-Daemon",
        now=_CREATED_AT + timedelta(hours=2),
    )

    assert [entry.id for entry in active_entries] == ["active"]


def test_conflicting_entries_are_detected_for_review(tmp_path: Path) -> None:
    store = RepoMemoryStore(tmp_path)
    store.add_entry(_entry("m1", body="Run tests with pytest."))

    conflicts = store.find_conflicts(_entry("m2", body="Run tests with uv run pytest."))

    assert [conflict.id for conflict in conflicts] == ["m1"]


def test_snapshot_selection_honors_repo_scope_issue_scope_and_limits(
    tmp_path: Path,
) -> None:
    store = RepoMemoryStore(tmp_path)
    store.add_entry(_entry("repo-1", body="Repository fact."))
    store.add_entry(_entry("issue-1", scope="issue", work_item_id="397", body="Issue fact."))
    store.add_entry(
        _entry("issue-2", scope="issue", work_item_id="999", body="Unrelated issue fact.")
    )
    store.add_entry(_entry("other-repo", repo_id="other/repo", body="Other repo fact."))

    snapshot = select_memory_snapshot(
        store.list_entries(repo_id="D-sorganization/Maxwell-Daemon", include_superseded=True),
        repo_id="D-sorganization/Maxwell-Daemon",
        work_item_id="397",
        max_items=2,
        token_budget=8,
    )

    assert [entry.id for entry in snapshot.entries] == ["repo-1", "issue-1"]
    assert snapshot.token_budget == 8
    assert snapshot.selection_reasons == {
        "repo-1": "repo match",
        "issue-1": "work item match",
    }


def test_accepted_repo_memory_can_be_loaded_by_another_store_instance(
    tmp_path: Path,
) -> None:
    first = RepoMemoryStore(tmp_path)
    first.add_entry(_entry("m1"))

    second = RepoMemoryStore(tmp_path)

    assert second.list_entries(repo_id="D-sorganization/Maxwell-Daemon") == [_entry("m1")]


def test_snapshot_render_includes_selection_reasons(tmp_path: Path) -> None:
    store = RepoMemoryStore(tmp_path)
    store.add_entry(_entry("repo-1", body="Repository fact."))
    store.add_entry(_entry("issue-1", scope="issue", work_item_id="397", body="Issue fact."))

    rendered = store.render_snapshot(
        repo_id="D-sorganization/Maxwell-Daemon",
        work_item_id="397",
        max_items=2,
        token_budget=128,
    )

    assert "Repo memory snapshot" in rendered
    assert "repo-1" in rendered
    assert "work item" in rendered


def test_snapshot_render_truncates_to_max_chars(tmp_path: Path) -> None:
    store = RepoMemoryStore(tmp_path)
    store.add_entry(_entry("repo-1", body="Repository fact " * 20))

    rendered = store.render_snapshot(
        repo_id="D-sorganization/Maxwell-Daemon",
        token_budget=128,
        max_chars=80,
    )

    assert rendered.endswith("... (truncated)")


def test_secret_redaction_masks_values_for_display() -> None:
    redacted = redact_secret_looking_values(
        "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456 and ghp_abcdefghijklmnopqrstuv"
    )

    assert "[REDACTED]" in redacted
    assert "sk-proj" not in redacted
