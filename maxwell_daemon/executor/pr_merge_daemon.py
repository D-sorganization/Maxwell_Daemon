"""Automates the post-PR-open merge pipeline with strict safety rails.

Auto-merging a PR is irreversible: once it lands on a protected branch, the
daemon can't unmerge. So the default posture is paranoid. Every gate below
must pass before the shepherd touches the PR:

  1. ``PrMergeConfig.enabled`` must be True (disabled by default).
  2. PR carries the ``required_label`` (opt-in marker).
  3. PR is not a draft.
  4. PR's base branch is on the ``allowed_base_branches`` allow-list.
  5. PR isn't already merged.
  6. Mergeable state is ``clean`` (or ``behind``, in which case we refresh
     the branch instead of merging).

If a gate fails we record a :class:`PrShepherdResult` with the appropriate
:class:`PrMergeDecision` and take no side-effecting action. This is
intentionally boring — the daemon's job is to skip, not to gamble.

DbC: the shepherd is a *pure decision* relative to its inputs. Every branch
returns a `PrShepherdResult`; no raising on policy outcomes. Failures of
the injected ``gh`` client do bubble up so the caller can back off.

LOD: the gh client is duck-typed — any object with the four methods we use
satisfies the protocol. No reach-through into the real GitHubClient.
"""

from __future__ import annotations

import logging
from maxwell_daemon.logging import get_logger
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

__all__ = [
    "PrMergeConfig",
    "PrMergeDaemon",
    "PrMergeDecision",
    "PrShepherdResult",
]

log = get_logger(__name__)

_TERMINAL_UNSUCCESSFUL_CONCLUSIONS = frozenset(
    {
        "action_required",
        "cancelled",
        "failure",
        "stale",
        "startup_failure",
        "timed_out",
    }
)
_PENDING_CHECK_STATUSES = frozenset({"in_progress", "pending", "queued", "requested", "waiting"})


class PrMergeDecision(str, Enum):
    """Outcome of one shepherd pass on a single PR."""

    DISABLED = "disabled"
    DRY_RUN = "dry_run"
    SKIPPED_NO_LABEL = "skipped_no_label"
    SKIPPED_DRAFT = "skipped_draft"
    SKIPPED_BASE_BRANCH = "skipped_base_branch"
    ENABLED_AUTO_MERGE = "enabled_auto_merge"
    ALREADY_ENABLED = "already_enabled"
    UPDATED_BRANCH = "updated_branch"
    WAITING_FOR_CI = "waiting_for_ci"
    CI_FAILED = "ci_failed"
    MERGED = "merged"
    UNKNOWN_STATE = "unknown_state"


@dataclass(slots=True, frozen=True)
class PrShepherdResult:
    repo: str
    number: int
    decision: PrMergeDecision
    detail: str = ""


@dataclass(slots=True, frozen=True)
class PrMergeConfig:
    """Policy for the PR auto-merge daemon. Off by default."""

    enabled: bool = False
    #: Only PRs carrying this label are considered.
    required_label: str = "maxwell:auto-merge-ok"
    #: Only PRs targeting one of these branches are considered.
    allowed_base_branches: tuple[str, ...] = ("staging",)
    #: Merge method: squash | merge | rebase.
    merge_method: str = "squash"
    #: When True, the shepherd logs what it *would* do without touching GitHub.
    dry_run: bool = False
    #: Poll interval between shepherd passes when the daemon is running as
    #: a loop. The shepherd itself is one-shot; the loop is an outer helper.
    poll_interval_seconds: float = 60.0


class _PrStateLike(Protocol):
    """What we read off a PR state object."""

    repo: str
    number: int
    base_branch: str
    draft: bool
    merged: bool
    auto_merge_enabled: bool
    mergeable_state: str
    labels: list[str]
    head_sha: str
    check_runs: list[dict[str, str]]


class _GhLike(Protocol):
    async def get_pr(self, repo: str, number: int) -> Any: ...
    async def get_check_runs(self, repo: str, head_sha: str) -> list[dict[str, str]]: ...
    async def enable_auto_merge(self, repo: str, number: int, *, method: str) -> None: ...
    async def update_branch(self, repo: str, number: int) -> None: ...
    async def add_label(self, repo: str, number: int, label: str) -> None: ...


class PrMergeDaemon:
    """Polls open PRs and shepherds them through auto-merge."""

    def __init__(self, *, config: PrMergeConfig) -> None:
        self._config = config

    async def shepherd(self, pr: _PrStateLike, *, gh: _GhLike) -> PrShepherdResult:
        """Run one pass of the merge pipeline against ``pr``.

        Returns a :class:`PrShepherdResult` in every branch. We never raise
        on a policy outcome — that's what the ``decision`` enum is for.
        """
        cfg = self._config

        if not cfg.enabled:
            return PrShepherdResult(
                repo=pr.repo, number=pr.number, decision=PrMergeDecision.DISABLED
            )

        if cfg.dry_run:
            return PrShepherdResult(
                repo=pr.repo,
                number=pr.number,
                decision=PrMergeDecision.DRY_RUN,
                detail="dry_run=True — no GitHub calls made",
            )

        if pr.merged:
            return PrShepherdResult(repo=pr.repo, number=pr.number, decision=PrMergeDecision.MERGED)

        if cfg.required_label not in pr.labels:
            return PrShepherdResult(
                repo=pr.repo,
                number=pr.number,
                decision=PrMergeDecision.SKIPPED_NO_LABEL,
                detail=f"missing required label {cfg.required_label!r}",
            )

        if pr.draft:
            return PrShepherdResult(
                repo=pr.repo, number=pr.number, decision=PrMergeDecision.SKIPPED_DRAFT
            )

        if pr.base_branch not in cfg.allowed_base_branches:
            return PrShepherdResult(
                repo=pr.repo,
                number=pr.number,
                decision=PrMergeDecision.SKIPPED_BASE_BRANCH,
                detail=(
                    f"base {pr.base_branch!r} not in allow-list {list(cfg.allowed_base_branches)}"
                ),
            )

        # Behind: refresh the branch. We intentionally don't enable auto-merge
        # first — let the next cycle catch the refreshed state.
        if pr.mergeable_state == "behind":
            await gh.update_branch(pr.repo, pr.number)
            return PrShepherdResult(
                repo=pr.repo, number=pr.number, decision=PrMergeDecision.UPDATED_BRANCH
            )

        # Blocked: dig into check runs to decide between "wait" and "give up".
        if pr.mergeable_state == "blocked":
            decision = self._classify_blocked(pr.check_runs)
            return PrShepherdResult(repo=pr.repo, number=pr.number, decision=decision)

        # Clean: enable auto-merge (idempotent via auto_merge_enabled flag).
        if pr.mergeable_state == "clean":
            if pr.auto_merge_enabled:
                return PrShepherdResult(
                    repo=pr.repo,
                    number=pr.number,
                    decision=PrMergeDecision.ALREADY_ENABLED,
                )
            await gh.enable_auto_merge(pr.repo, pr.number, method=cfg.merge_method)
            return PrShepherdResult(
                repo=pr.repo,
                number=pr.number,
                decision=PrMergeDecision.ENABLED_AUTO_MERGE,
            )

        # Anything else (dirty, unknown) — wait.
        return PrShepherdResult(
            repo=pr.repo,
            number=pr.number,
            decision=PrMergeDecision.UNKNOWN_STATE,
            detail=f"mergeable_state={pr.mergeable_state!r}",
        )

    @staticmethod
    def _classify_blocked(check_runs: Iterable[dict[str, str]]) -> PrMergeDecision:
        """Decide between ``CI_FAILED`` and ``WAITING_FOR_CI`` given a check-run snapshot."""
        checks = list(check_runs)
        if any(
            (check.get("conclusion") or "").lower() in _TERMINAL_UNSUCCESSFUL_CONCLUSIONS
            for check in checks
        ):
            return PrMergeDecision.CI_FAILED
        if not checks:
            return PrMergeDecision.WAITING_FOR_CI
        if any((check.get("status") or "").lower() in _PENDING_CHECK_STATUSES for check in checks):
            return PrMergeDecision.WAITING_FOR_CI
        return PrMergeDecision.UNKNOWN_STATE

    # ── Optional: batch shepherd across many PRs ────────────────────────────

    async def shepherd_all(
        self, prs: Iterable[_PrStateLike], *, gh: _GhLike
    ) -> list[PrShepherdResult]:
        """Shepherd every PR in ``prs`` sequentially; return every result.

        Sequential rather than ``asyncio.gather`` because GitHub's abuse-rate
        limits punish bursty auto-merge traffic — we'd rather one PR at a
        time than risk a rate-limit black-out.
        """
        results: list[PrShepherdResult] = []
        for pr in prs:
            try:
                results.append(await self.shepherd(pr, gh=gh))
            except Exception:
                log.warning("shepherd raised for pr=%s/%s", pr.repo, pr.number, exc_info=True)
                results.append(
                    PrShepherdResult(
                        repo=pr.repo,
                        number=pr.number,
                        decision=PrMergeDecision.UNKNOWN_STATE,
                        detail="shepherd raised — see logs",
                    )
                )
        return results
