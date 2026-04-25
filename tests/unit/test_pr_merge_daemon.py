"""Tests for PrMergeDaemon — automates post-open PR merge with strict safety rails.

Auto-merging is irreversible, so the daemon's default posture is paranoid:

  * Disabled by default — must be opt-in per config flag.
  * PR must carry a specific "I've been authorised to auto-merge" label.
  * PR must not be a draft.
  * Target branch must be on the allow-list (typically ``staging``).
  * Any missing required CI check blocks the merge attempt.

These tests cover each gate plus the happy-path shepherd flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from maxwell_daemon.executor.pr_merge_daemon import (
    PrMergeConfig,
    PrMergeDaemon,
    PrMergeDecision,
    PrShepherdResult,
)

# ── Test doubles ─────────────────────────────────────────────────────────────


@dataclass
class _StubPr:
    repo: str
    number: int
    head_sha: str = "abc123"
    base_branch: str = "staging"
    draft: bool = False
    merged: bool = False
    auto_merge_enabled: bool = False
    mergeable_state: str = "clean"  # clean | behind | blocked | dirty | unknown
    labels: list[str] = field(default_factory=list)
    required_checks: list[str] = field(default_factory=list)
    check_runs: list[dict[str, str]] = field(default_factory=list)


class _StubGh:
    """Records actions, serves canned PR state."""

    def __init__(self, pr: _StubPr) -> None:
        self.pr = pr
        self.merge_calls: list[dict[str, Any]] = []
        self.update_branch_calls: list[dict[str, Any]] = []
        self.label_calls: list[dict[str, Any]] = []

    async def get_pr(self, repo: str, number: int) -> _StubPr:
        return self.pr

    async def get_check_runs(self, repo: str, head_sha: str) -> list[dict[str, str]]:
        return list(self.pr.check_runs)

    async def enable_auto_merge(self, repo: str, number: int, *, method: str) -> None:
        self.merge_calls.append({"repo": repo, "number": number, "method": method})
        self.pr.auto_merge_enabled = True

    async def update_branch(self, repo: str, number: int) -> None:
        self.update_branch_calls.append({"repo": repo, "number": number})

    async def add_label(self, repo: str, number: int, label: str) -> None:
        self.label_calls.append({"repo": repo, "number": number, "label": label})


def _default_config(**overrides: Any) -> PrMergeConfig:
    base = {
        "enabled": True,
        "required_label": "maxwell:auto-merge-ok",
        "allowed_base_branches": ("staging",),
        "merge_method": "squash",
    }
    base.update(overrides)
    return PrMergeConfig(**base)  # type: ignore[arg-type]


# ── Daemon off by default ────────────────────────────────────────────────────


class TestDisabledByDefault:
    def test_default_config_is_disabled(self) -> None:
        cfg = PrMergeConfig()
        assert cfg.enabled is False

    async def test_shepherd_is_no_op_when_disabled(self) -> None:
        pr = _StubPr(repo="a/b", number=1, labels=["maxwell:auto-merge-ok"])
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=PrMergeConfig(enabled=False))
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.DISABLED
        assert gh.merge_calls == []
        assert gh.update_branch_calls == []


# ── Gate: required label ─────────────────────────────────────────────────────


class TestRequiredLabel:
    async def test_missing_label_skips(self) -> None:
        pr = _StubPr(repo="a/b", number=1, labels=[])  # no label
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config())
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.SKIPPED_NO_LABEL
        assert gh.merge_calls == []

    async def test_wrong_label_skips(self) -> None:
        pr = _StubPr(repo="a/b", number=1, labels=["just-a-bug"])
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config())
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.SKIPPED_NO_LABEL


# ── Gate: draft PRs ──────────────────────────────────────────────────────────


class TestDraftsSkipped:
    async def test_draft_pr_skipped_even_with_label(self) -> None:
        pr = _StubPr(repo="a/b", number=1, draft=True, labels=["maxwell:auto-merge-ok"])
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config())
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.SKIPPED_DRAFT
        assert gh.merge_calls == []


# ── Gate: base branch allow-list ────────────────────────────────────────────


class TestBaseBranchAllowList:
    async def test_disallowed_base_branch_skipped(self) -> None:
        pr = _StubPr(
            repo="a/b",
            number=1,
            base_branch="main",  # not on allow-list (only staging is)
            labels=["maxwell:auto-merge-ok"],
        )
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config())
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.SKIPPED_BASE_BRANCH
        assert gh.merge_calls == []

    async def test_explicit_main_allowed_when_configured(self) -> None:
        pr = _StubPr(
            repo="a/b",
            number=1,
            base_branch="main",
            labels=["maxwell:auto-merge-ok"],
        )
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config(allowed_base_branches=("staging", "main")))
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision in {
            PrMergeDecision.ENABLED_AUTO_MERGE,
            PrMergeDecision.ALREADY_ENABLED,
            PrMergeDecision.MERGED,
        }


# ── Gate: already merged ────────────────────────────────────────────────────


class TestAlreadyMerged:
    async def test_merged_pr_is_recorded(self) -> None:
        pr = _StubPr(
            repo="a/b",
            number=1,
            merged=True,
            labels=["maxwell:auto-merge-ok"],
        )
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config())
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.MERGED
        assert gh.merge_calls == []


# ── Happy path ──────────────────────────────────────────────────────────────


class TestHappyPath:
    async def test_clean_pr_gets_auto_merge_enabled(self) -> None:
        pr = _StubPr(
            repo="a/b",
            number=1,
            mergeable_state="clean",
            labels=["maxwell:auto-merge-ok"],
        )
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config())
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.ENABLED_AUTO_MERGE
        assert gh.merge_calls == [{"repo": "a/b", "number": 1, "method": "squash"}]

    async def test_already_enabled_is_noted_not_retriggered(self) -> None:
        pr = _StubPr(
            repo="a/b",
            number=1,
            mergeable_state="clean",
            auto_merge_enabled=True,
            labels=["maxwell:auto-merge-ok"],
        )
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config())
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.ALREADY_ENABLED
        assert gh.merge_calls == []  # not re-enabled


# ── Behind: update branch ───────────────────────────────────────────────────


class TestBehindBranchHandling:
    async def test_behind_pr_gets_update_branch_called(self) -> None:
        pr = _StubPr(
            repo="a/b",
            number=1,
            mergeable_state="behind",
            auto_merge_enabled=True,
            labels=["maxwell:auto-merge-ok"],
        )
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config())
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.UPDATED_BRANCH
        assert gh.update_branch_calls == [{"repo": "a/b", "number": 1}]


# ── Blocked: CI failure vs still running ────────────────────────────────────


class TestBlockedState:
    @pytest.mark.parametrize(
        "conclusion",
        [
            "failure",
            "cancelled",
            "timed_out",
            "action_required",
            "startup_failure",
            "stale",
        ],
    )
    async def test_blocked_with_terminal_unsuccessful_check_marks_ci_failed(
        self, conclusion: str
    ) -> None:
        pr = _StubPr(
            repo="a/b",
            number=1,
            mergeable_state="blocked",
            labels=["maxwell:auto-merge-ok"],
            check_runs=[{"name": "ci", "status": "completed", "conclusion": conclusion}],
        )
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config())
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.CI_FAILED

    @pytest.mark.parametrize("status", ["queued", "in_progress"])
    async def test_blocked_with_pending_checks_means_wait(self, status: str) -> None:
        pr = _StubPr(
            repo="a/b",
            number=1,
            mergeable_state="blocked",
            labels=["maxwell:auto-merge-ok"],
            check_runs=[{"name": "ci", "status": status, "conclusion": ""}],
        )
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config())
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.WAITING_FOR_CI

    async def test_blocked_with_no_check_snapshot_means_wait(self) -> None:
        pr = _StubPr(
            repo="a/b",
            number=1,
            mergeable_state="blocked",
            labels=["maxwell:auto-merge-ok"],
            check_runs=[],
        )
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config())
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.WAITING_FOR_CI

    async def test_blocked_with_only_terminal_successful_checks_is_unknown(
        self,
    ) -> None:
        pr = _StubPr(
            repo="a/b",
            number=1,
            mergeable_state="blocked",
            labels=["maxwell:auto-merge-ok"],
            check_runs=[{"name": "ci", "status": "completed", "conclusion": "success"}],
        )
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config())
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.UNKNOWN_STATE


# ── Dry-run ─────────────────────────────────────────────────────────────────


class TestDryRun:
    async def test_dry_run_never_calls_gh_actions(self) -> None:
        pr = _StubPr(
            repo="a/b",
            number=1,
            mergeable_state="behind",
            labels=["maxwell:auto-merge-ok"],
        )
        gh = _StubGh(pr)
        daemon = PrMergeDaemon(config=_default_config(dry_run=True))
        result = await daemon.shepherd(pr, gh=gh)
        assert result.decision == PrMergeDecision.DRY_RUN
        assert gh.merge_calls == []
        assert gh.update_branch_calls == []


# ── Shape ────────────────────────────────────────────────────────────────────


class TestResultShape:
    def test_result_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        r = PrShepherdResult(repo="a/b", number=1, decision=PrMergeDecision.DISABLED)
        with pytest.raises(FrozenInstanceError):
            r.number = 2  # type: ignore[misc]
