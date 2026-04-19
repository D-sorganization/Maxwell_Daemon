"""IssueExecutor — converts a GitHub issue into a draft PR via an LLM.

Orchestration only. All external side-effects — HTTP to GitHub, subprocess git,
LLM requests — go through injected collaborators so the executor is pure
control flow that's easy to test.

Modes:
  * ``plan``      — fetch issue, ask LLM for a plan, open an empty draft PR
                    seeded with that plan. Safe — no code ever written.
  * ``implement`` — ask the LLM for a unified diff, apply it to a fresh branch,
                    commit, push, open a draft PR. Still human-reviewed before
                    merge because the PR is opened as a draft.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from conductor.backends.base import ILLMBackend, Message, MessageRole
from conductor.gh.workspace import WorkspaceError

__all__ = ["IssueExecutionError", "IssueExecutor", "IssueResult"]

Mode = Literal["plan", "implement"]

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


class IssueExecutionError(RuntimeError):
    """Raised when issue → PR execution can't complete."""


@dataclass(slots=True, frozen=True)
class IssueResult:
    issue_number: int
    pr_url: str
    pr_number: int
    plan: str
    applied_diff: bool


class _GitHubProto(Protocol):
    async def get_issue(self, repo: str, number: int) -> Any: ...
    async def create_pull_request(
        self,
        repo: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool = True,
    ) -> Any: ...


class _WorkspaceProto(Protocol):
    async def ensure_clone(self, repo: str) -> Any: ...
    async def create_branch(self, repo: str, branch: str, *, base: str = "main") -> None: ...
    async def apply_diff(self, repo: str, diff: str) -> None: ...
    async def commit_and_push(self, repo: str, *, branch: str, message: str) -> None: ...


_SYSTEM_PROMPT = """You are a senior engineer drafting a pull request for a GitHub issue.

Respond with a single JSON object on its own:

{
  "plan": "A concise Markdown description of what the change does and why (shown in the PR body)",
  "diff": "A unified diff suitable for `git apply --index`. Empty string if no code change is appropriate yet."
}

Rules:
- The diff must use proper unified-diff format with `diff --git`, `---`, `+++`, and `@@` hunk headers.
- Never include files you haven't seen. Prefer small, surgical changes over sweeping rewrites.
- If you're unsure, return an empty diff and explain what's missing in the plan.
"""


class IssueExecutor:
    def __init__(
        self,
        *,
        github: _GitHubProto,
        workspace: _WorkspaceProto,
        backend: ILLMBackend,
        max_diff_retries: int = 2,
    ) -> None:
        self._gh = github
        self._ws = workspace
        self._backend = backend
        self._max_diff_retries = max_diff_retries

    async def execute_issue(
        self,
        *,
        repo: str,
        issue_number: int,
        model: str,
        mode: Mode = "plan",
        base_branch: str = "main",
    ) -> IssueResult:
        issue = await self._gh.get_issue(repo, issue_number)
        branch = f"conductor/issue-{issue_number}"

        plan, diff = await self._draft_change(
            issue_title=issue.title, issue_body=issue.body, model=model
        )

        applied = False
        if mode == "implement":
            if not diff.strip():
                raise IssueExecutionError(
                    "LLM returned no diff but mode=implement — rerun in plan mode "
                    "or refine the issue."
                )
            await self._ws.ensure_clone(repo)
            await self._ws.create_branch(repo, branch, base=base_branch)
            plan, diff = await self._apply_with_retry(
                repo=repo,
                issue_title=issue.title,
                issue_body=issue.body,
                model=model,
                plan=plan,
                diff=diff,
            )
            await self._ws.commit_and_push(
                repo,
                branch=branch,
                message=f"Fix #{issue_number}: {issue.title}",
            )
            applied = True

        pr_body = self._format_pr_body(issue_number=issue_number, plan=plan, applied=applied)
        pr = await self._gh.create_pull_request(
            repo,
            head=branch,
            base=base_branch,
            title=f"Fix #{issue_number}: {issue.title}",
            body=pr_body,
            draft=True,
        )
        return IssueResult(
            issue_number=issue_number,
            pr_url=pr.url,
            pr_number=pr.number,
            plan=plan,
            applied_diff=applied,
        )

    async def _apply_with_retry(
        self,
        *,
        repo: str,
        issue_title: str,
        issue_body: str,
        model: str,
        plan: str,
        diff: str,
    ) -> tuple[str, str]:
        """Try to apply the diff; on failure, ask the LLM for a corrected diff."""
        attempts = 0
        last_error: str = ""
        current_plan, current_diff = plan, diff
        while True:
            try:
                await self._ws.apply_diff(repo, current_diff)
                return current_plan, current_diff
            except WorkspaceError as e:
                last_error = str(e)
                attempts += 1
                if attempts > self._max_diff_retries:
                    raise IssueExecutionError(
                        f"diff did not apply after {attempts} attempt(s); last error: {last_error}"
                    ) from e
                current_plan, current_diff = await self._refine_diff(
                    issue_title=issue_title,
                    issue_body=issue_body,
                    model=model,
                    previous_plan=current_plan,
                    previous_diff=current_diff,
                    error=last_error,
                )

    async def _refine_diff(
        self,
        *,
        issue_title: str,
        issue_body: str,
        model: str,
        previous_plan: str,
        previous_diff: str,
        error: str,
    ) -> tuple[str, str]:
        prompt = (
            f"Your previous diff did not apply cleanly.\n\n"
            f"Issue title: {issue_title}\n"
            f"Issue body:\n{issue_body or '(empty)'}\n\n"
            f"Your previous plan:\n{previous_plan}\n\n"
            f"Your previous diff:\n{previous_diff}\n\n"
            f"git apply failed with: {error}\n\n"
            "Return a corrected JSON object (same schema) with a diff that applies."
        )
        response = await self._backend.complete(
            [
                Message(role=MessageRole.SYSTEM, content=_SYSTEM_PROMPT),
                Message(role=MessageRole.USER, content=prompt),
            ],
            model=model,
            temperature=0.2,
        )
        return self._parse_response(response.content)

    async def _draft_change(
        self, *, issue_title: str, issue_body: str, model: str
    ) -> tuple[str, str]:
        prompt = (
            f"Issue title: {issue_title}\n\n"
            f"Issue body:\n{issue_body or '(empty)'}\n\n"
            "Produce the JSON plan now."
        )
        response = await self._backend.complete(
            [
                Message(role=MessageRole.SYSTEM, content=_SYSTEM_PROMPT),
                Message(role=MessageRole.USER, content=prompt),
            ],
            model=model,
            temperature=0.2,
        )
        plan, diff = self._parse_response(response.content)
        return plan, diff

    @staticmethod
    def _parse_response(raw: str) -> tuple[str, str]:
        content = raw.strip()
        fence_match = _FENCE_RE.match(content)
        if fence_match:
            content = fence_match.group(1).strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            raise IssueExecutionError(f"Could not parse LLM response as JSON: {e}") from e

        plan = str(parsed.get("plan", "")).strip()
        diff = str(parsed.get("diff", "")).strip()
        if not plan:
            raise IssueExecutionError("LLM response missing non-empty 'plan' field")
        return plan, diff

    @staticmethod
    def _format_pr_body(*, issue_number: int, plan: str, applied: bool) -> str:
        header = (
            f"Closes #{issue_number}\n\n"
            f"> 🤖 Drafted by CONDUCTOR — "
            f"{'code applied' if applied else 'plan only'}.\n\n"
        )
        return header + plan
