"""MemoryManager — composite the IssueExecutor actually talks to.

Assembles context from the three memory tiers with a single token budget, and
exposes a ``record_outcome`` hook that persists new facts + an episode when a
task ends successfully.
"""

from __future__ import annotations

from maxwell_daemon.memory.episodic import Episode, EpisodicStore
from maxwell_daemon.memory.profile import RepoProfile
from maxwell_daemon.memory.scratchpad import ScratchPad

__all__ = ["MemoryManager"]


class MemoryManager:
    def __init__(
        self,
        *,
        scratchpad: ScratchPad | None = None,
        profile: RepoProfile | None = None,
        episodes: EpisodicStore | None = None,
    ) -> None:
        # All three are optional so tests can wire in just the tiers they need.
        self.scratchpad = scratchpad or ScratchPad()
        self.profile = profile
        self.episodes = episodes

    def assemble_context(
        self,
        *,
        repo: str,
        issue_title: str,
        issue_body: str,
        task_id: str,
        max_chars: int = 8000,
    ) -> str:
        """Render the memory tiers into a single markdown block.

        Budget split: profile 25%, scratchpad 25%, episodes 50% — the episodic
        store is the highest-leverage tier per byte since it carries full prior
        plans.
        """
        budget_profile = max(400, int(max_chars * 0.25))
        budget_scratch = max(400, int(max_chars * 0.25))
        budget_episodes = max(600, int(max_chars * 0.50))

        parts: list[str] = []

        profile_text = self.profile.render(repo, max_chars=budget_profile) if self.profile else ""
        if profile_text:
            parts.append("## Repo facts (from prior runs)\n")
            parts.append(profile_text)

        scratch_text = self.scratchpad.render(task_id, max_chars=budget_scratch)
        if scratch_text:
            parts.append("\n## Scratchpad (this task's history)\n")
            parts.append(scratch_text)

        if self.episodes is not None:
            related = self.episodes.render_related(
                f"{issue_title} {issue_body}", repo=repo, limit=3
            )
            if related:
                parts.append("\n## Related past issues\n")
                parts.append(related[:budget_episodes])

        rendered = "\n".join(parts)
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars] + "\n... (truncated)"
        return rendered

    def record_outcome(
        self,
        *,
        task_id: str,
        repo: str,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        plan: str,
        applied_diff: bool,
        pr_url: str,
        outcome: str,
    ) -> None:
        """Persist an episode and drop the per-task scratchpad."""
        if self.episodes is not None:
            self.episodes.record(
                Episode(
                    id=task_id,
                    repo=repo,
                    issue_number=issue_number,
                    issue_title=issue_title,
                    issue_body=issue_body,
                    plan=plan,
                    applied_diff=applied_diff,
                    pr_url=pr_url,
                    outcome=outcome,
                )
            )
        self.scratchpad.clear(task_id)
