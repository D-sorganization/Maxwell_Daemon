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

import inspect
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from maxwell_daemon.backends.base import ILLMBackend, Message, MessageRole
from maxwell_daemon.core.artifacts import ArtifactKind
from maxwell_daemon.core.repo_overrides import RepoOverrides
from maxwell_daemon.gh.context import ContextBuilder
from maxwell_daemon.gh.test_runner import TestResult, TestRunner
from maxwell_daemon.gh.workspace import WorkspaceError
from maxwell_daemon.memory import MemoryBackend
from maxwell_daemon.templates import classify_issue, render_system_prompt
from maxwell_daemon.tracing import span as _trace_span

__all__ = ["IssueExecutionError", "IssueExecutor", "IssueResult"]

Mode = Literal["plan", "implement"]


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


class _GitHubBranchProto(Protocol):
    """Minimal protocol for branch resolution — used by resolve_pr_target_branch."""

    async def list_branches(self, repo: str) -> list[str]: ...
    async def get_default_branch(self, repo: str) -> str: ...


class _WorkspaceProto(Protocol):
    async def ensure_clone(self, repo: str, *, task_id: str) -> Any: ...
    async def create_branch(
        self, repo: str, branch: str, *, base: str = "main", task_id: str
    ) -> None: ...
    async def apply_diff(self, repo: str, diff: str, *, task_id: str) -> None: ...
    async def commit_and_push(
        self, repo: str, *, branch: str, message: str, task_id: str
    ) -> None: ...


class _ArtifactSinkProto(Protocol):
    def put_text(
        self,
        *,
        kind: ArtifactKind,
        name: str,
        text: str,
        task_id: str | None = None,
        work_item_id: str | None = None,
        media_type: str = "text/plain; charset=utf-8",
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...


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
        context_builder: ContextBuilder | None = None,
        context_max_chars: int = 16_000,
        test_runner: TestRunner | Any | None = None,
        max_test_retries: int = 1,
        test_timeout_seconds: float = 300.0,
        memory: MemoryBackend | None = None,
        memory_max_chars: int = 8000,
        artifact_store: _ArtifactSinkProto | None = None,
    ) -> None:
        self._gh = github
        self._ws = workspace
        self._backend = backend
        self._max_diff_retries = max_diff_retries
        self._context_builder = context_builder
        self._context_max_chars = context_max_chars
        self._test_runner = test_runner
        self._max_test_retries = max_test_retries
        self._test_timeout = test_timeout_seconds
        self._memory = memory
        self._memory_max_chars = memory_max_chars
        self._artifact_store = artifact_store

    @staticmethod
    def _build_system_prompt(
        *,
        overrides: RepoOverrides | None,
        repo: str,
        issue_title: str,
        issue_body: str,
    ) -> str:
        """Build the effective system prompt, prepending any per-repo prefix."""
        if overrides is None:
            return _SYSTEM_PROMPT

        prefix = ""
        if overrides.system_prompt_file is not None:
            prefix = overrides.system_prompt_file.read_text(encoding="utf-8")
        elif overrides.system_prompt_prefix is not None:
            prefix = overrides.system_prompt_prefix

        if not prefix:
            return _SYSTEM_PROMPT

        prefix = prefix.format(
            repo=repo,
            issue_title=issue_title,
            issue_body=(issue_body or "")[:500],
        )
        return (prefix + "\n\n" + _SYSTEM_PROMPT).strip()

    async def execute_issue(
        self,
        *,
        repo: str,
        issue_number: int,
        model: str,
        mode: Mode = "plan",
        base_branch: str = "main",
        overrides: RepoOverrides | None = None,
        task_id: str | None = None,
        on_test_output: Any = None,
    ) -> IssueResult:
        async with _trace_span(
            "maxwell_daemon.issue.fetch",
            {"repo": repo, "issue": issue_number},
        ):
            issue = await self._gh.get_issue(repo, issue_number)
        branch = f"maxwell-daemon/issue-{issue_number}"
        # Fall back to a derived id when the caller didn't supply one so the
        # executor stays usable in non-Daemon contexts (one-off scripts, tests).
        effective_task_id = task_id or f"issue-{issue_number}"

        # Resolve per-call settings: overrides first, executor defaults second.
        ctx_max = self._pick(overrides, "context_max_chars", self._context_max_chars)
        max_diff_retries = self._pick(overrides, "max_diff_retries", self._max_diff_retries)
        max_test_retries = self._pick(overrides, "max_test_retries", self._max_test_retries)
        test_command = overrides.test_command if overrides else None

        # Build effective system prompt from per-repo override (if any).
        effective_system_prompt = self._build_system_prompt(
            overrides=overrides,
            repo=repo,
            issue_title=issue.title,
            issue_body=issue.body,
        )

        # Build context if we have a builder AND we're in implement mode (plan
        # mode doesn't need a clone; enable via a follow-up if it helps).
        context_prompt = ""
        if mode == "implement" and self._context_builder is not None:
            repo_path = await self._ws.ensure_clone(repo, task_id=effective_task_id)
            ctx = await self._context_builder.build(
                repo_path,
                issue.body,
                repo_id=repo,
                issue_title=issue.title,
                issue_number=issue_number,
            )
            context_prompt = ctx.to_prompt(max_chars=ctx_max)

        # Memory: assemble repo profile + related episodes + scratchpad, if any.
        memory_prompt = ""
        if self._memory is not None:
            memory_prompt = await self._assemble_memory_context(
                repo=repo,
                issue_title=issue.title,
                issue_body=issue.body,
                task_id=effective_task_id,
                max_chars=self._memory_max_chars,
            )

        async with _trace_span(
            "maxwell_daemon.issue.draft",
            {"repo": repo, "issue": issue_number, "model": model, "mode": mode},
        ):
            plan, diff = await self._draft_change(
                issue_title=issue.title,
                issue_body=issue.body,
                model=model,
                context=context_prompt,
                memory=memory_prompt,
                labels=list(getattr(issue, "labels", []) or []),
                system_prompt=effective_system_prompt,
            )

        self._record_artifact(
            task_id=effective_task_id,
            kind=ArtifactKind.PLAN,
            name="Initial plan",
            text=plan,
            media_type="text/markdown",
            metadata={
                "repo": repo,
                "issue_number": issue_number,
                "mode": mode,
                "model": model,
            },
        )
        self._record_artifact(
            task_id=effective_task_id,
            kind=ArtifactKind.DIFF,
            name="Initial diff",
            text=diff,
            media_type="text/x-diff",
            metadata={
                "repo": repo,
                "issue_number": issue_number,
                "mode": mode,
                "model": model,
            },
        )

        # Record the initial plan to the scratchpad so retries see it.
        if self._memory is not None and plan:
            self._memory.scratchpad.append(effective_task_id, role="plan", content=plan)

        applied = False
        test_result: TestResult | None = None
        if mode == "implement":
            if not diff.strip():
                raise IssueExecutionError(
                    "LLM returned no diff but mode=implement — rerun in plan mode "
                    "or refine the issue."
                )
            # If we didn't already clone for context, clone now.
            if not context_prompt:
                await self._ws.ensure_clone(repo, task_id=effective_task_id)
            await self._ws.create_branch(repo, branch, base=base_branch, task_id=effective_task_id)
            plan, diff = await self._apply_with_retry(
                repo=repo,
                issue_title=issue.title,
                issue_body=issue.body,
                model=model,
                plan=plan,
                diff=diff,
                max_retries=max_diff_retries,
                task_id=effective_task_id,
                system_prompt=effective_system_prompt,
            )
            self._record_artifact(
                task_id=effective_task_id,
                kind=ArtifactKind.DIFF,
                name="Applied diff",
                text=diff,
                media_type="text/x-diff",
                metadata={
                    "repo": repo,
                    "issue_number": issue_number,
                    "mode": mode,
                    "model": model,
                },
            )
            if self._test_runner is not None:
                plan, diff, test_result = await self._validate_with_tests(
                    repo=repo,
                    branch=branch,
                    issue_title=issue.title,
                    issue_body=issue.body,
                    model=model,
                    plan=plan,
                    diff=diff,
                    base_branch=base_branch,
                    max_retries=max_test_retries,
                    test_command=test_command,
                    max_diff_retries=max_diff_retries,
                    task_id=effective_task_id,
                    on_test_output=on_test_output,
                    system_prompt=effective_system_prompt,
                )
                self._record_artifact(
                    task_id=effective_task_id,
                    kind=ArtifactKind.TEST_RESULT,
                    name="Test result",
                    text=json.dumps(
                        {
                            "passed": test_result.passed,
                            "command": test_result.command,
                            "returncode": test_result.returncode,
                            "duration_seconds": test_result.duration_seconds,
                            "output_tail": test_result.output_tail,
                        },
                        indent=2,
                        sort_keys=True,
                    ),
                    media_type="application/json",
                    metadata={
                        "repo": repo,
                        "issue_number": issue_number,
                        "mode": mode,
                    },
                )
            await self._ws.commit_and_push(
                repo,
                branch=branch,
                message=f"Fix #{issue_number}: {issue.title}",
                task_id=effective_task_id,
            )
            applied = True

        pr_body = self._format_pr_body(
            issue_number=issue_number,
            plan=plan,
            applied=applied,
            test_result=test_result,
        )
        self._record_artifact(
            task_id=effective_task_id,
            kind=ArtifactKind.PR_BODY,
            name="PR body",
            text=pr_body,
            media_type="text/markdown",
            metadata={
                "repo": repo,
                "issue_number": issue_number,
                "mode": mode,
            },
        )
        async with _trace_span(
            "maxwell_daemon.issue.open_pr",
            {"repo": repo, "issue": issue_number, "applied_diff": applied},
        ):
            pr = await self._gh.create_pull_request(
                repo,
                head=branch,
                base=base_branch,
                title=f"Fix #{issue_number}: {issue.title}",
                body=pr_body,
                draft=True,
            )

        # Memory write-back: one episode per successful PR open so future
        # related issues can retrieve it. Outcome is "completed" until/unless
        # the PR is later merged — we don't know that from here.
        if self._memory is not None:
            await self._record_memory_outcome(
                task_id=effective_task_id,
                repo=repo,
                issue_number=issue_number,
                issue_title=issue.title,
                issue_body=issue.body,
                plan=plan,
                applied_diff=applied,
                pr_url=pr.url,
                outcome="completed",
            )

        return IssueResult(
            issue_number=issue_number,
            pr_url=pr.url,
            pr_number=pr.number,
            plan=plan,
            applied_diff=applied,
        )

    async def _assemble_memory_context(
        self,
        *,
        repo: str,
        issue_title: str,
        issue_body: str,
        task_id: str,
        max_chars: int,
    ) -> str:
        if self._memory is None:
            return ""
        method = getattr(self._memory, "assemble_context_async", None)
        if callable(method):
            result = method(
                repo=repo,
                issue_title=issue_title,
                issue_body=issue_body,
                task_id=task_id,
                max_chars=max_chars,
            )
            if inspect.isawaitable(result):
                return cast(str, await result)
            if isinstance(result, str):
                return result
        return self._memory.assemble_context(
            repo=repo,
            issue_title=issue_title,
            issue_body=issue_body,
            task_id=task_id,
            max_chars=max_chars,
        )

    async def _record_memory_outcome(
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
        if self._memory is None:
            return
        method = getattr(self._memory, "record_outcome_async", None)
        if callable(method):
            result = method(
                task_id=task_id,
                repo=repo,
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body,
                plan=plan,
                applied_diff=applied_diff,
                pr_url=pr_url,
                outcome=outcome,
            )
            if inspect.isawaitable(result):
                await result
                return
            if result is None:
                return
        self._memory.record_outcome(
            task_id=task_id,
            repo=repo,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            plan=plan,
            applied_diff=applied_diff,
            pr_url=pr_url,
            outcome=outcome,
        )

    def _record_artifact(
        self,
        *,
        task_id: str,
        kind: ArtifactKind,
        name: str,
        text: str,
        media_type: str,
        metadata: dict[str, Any],
    ) -> None:
        if self._artifact_store is None:
            return
        self._artifact_store.put_text(
            task_id=task_id,
            kind=kind,
            name=name,
            text=text,
            media_type=media_type,
            metadata=metadata,
        )

    async def _validate_with_tests(
        self,
        *,
        repo: str,
        branch: str,
        issue_title: str,
        issue_body: str,
        model: str,
        plan: str,
        diff: str,
        base_branch: str,
        max_retries: int,
        test_command: list[str] | None,
        max_diff_retries: int,
        task_id: str,
        on_test_output: Any = None,
        system_prompt: str = _SYSTEM_PROMPT,
    ) -> tuple[str, str, TestResult]:
        """Run repo tests; if they fail, ask the LLM to refine the diff and retry.

        Returns the final (plan, diff, test_result). Raises if tests still fail
        after ``max_retries`` refinements.
        """
        assert self._test_runner is not None
        attempt = 0
        current_plan, current_diff = plan, diff
        repo_path = self._workspace_path(repo, task_id)

        while True:
            result = await self._test_runner.detect_and_run(
                repo_path,
                timeout=self._test_timeout,
                command=test_command,
                on_chunk=on_test_output,
            )
            if result.passed:
                return current_plan, current_diff, result
            attempt += 1
            if attempt > max_retries:
                raise IssueExecutionError(
                    f"tests still failing after {attempt} attempt(s); "
                    f"last output: {result.output_tail[-500:]}"
                )
            current_plan, current_diff = await self._refine_from_tests(
                issue_title=issue_title,
                issue_body=issue_body,
                model=model,
                previous_plan=current_plan,
                previous_diff=current_diff,
                test_output=result.output_tail,
                system_prompt=system_prompt,
            )
            await self._ws.create_branch(repo, branch, base=base_branch, task_id=task_id)
            await self._apply_with_retry(
                repo=repo,
                issue_title=issue_title,
                issue_body=issue_body,
                model=model,
                plan=current_plan,
                diff=current_diff,
                max_retries=max_diff_retries,
                task_id=task_id,
                system_prompt=system_prompt,
            )

    def _workspace_path(self, repo: str, task_id: str | None = None) -> Any:
        """Return the local checkout directory for ``(repo, task_id)``.

        Uses the workspace's ``path_for`` method when available (real
        Workspace) or falls back to a Path derived from the stub test double.
        """
        if hasattr(self._ws, "path_for"):
            try:
                return self._ws.path_for(repo, task_id=task_id or "test")
            except TypeError:
                # Legacy stub without task_id
                return self._ws.path_for(repo)
        # Namespace fallback paths by PID so parallel pytest workers
        # (pytest-xdist) that hit the stub-workspace branch don't clobber
        # each other's checkouts under a shared /tmp/maxwell-daemon-workspace.
        return (
            Path(tempfile.gettempdir())
            / f"maxwell-daemon-workspace-{os.getpid()}"
            / repo.split("/", 1)[1]
            / (task_id or "test")
        )

    async def _refine_from_tests(
        self,
        *,
        issue_title: str,
        issue_body: str,
        model: str,
        previous_plan: str,
        previous_diff: str,
        test_output: str,
        system_prompt: str = _SYSTEM_PROMPT,
    ) -> tuple[str, str]:
        prompt = (
            f"Your previous diff applied cleanly but the repo's own tests now fail.\n\n"
            f"Issue title: {issue_title}\n"
            f"Issue body:\n{issue_body or '(empty)'}\n\n"
            f"Your previous plan:\n{previous_plan}\n\n"
            f"Your previous diff:\n{previous_diff}\n\n"
            f"Test output (tail):\n{test_output}\n\n"
            "Return a corrected JSON object (same schema). Fix the failing tests."
        )
        response = await self._backend.complete(
            [
                Message(role=MessageRole.SYSTEM, content=system_prompt),
                Message(role=MessageRole.USER, content=prompt),
            ],
            model=model,
            temperature=0.2,
        )
        return self._parse_response(response.content)

    async def _apply_with_retry(
        self,
        *,
        repo: str,
        issue_title: str,
        issue_body: str,
        model: str,
        plan: str,
        diff: str,
        max_retries: int | None = None,
        task_id: str = "test",
        system_prompt: str = _SYSTEM_PROMPT,
    ) -> tuple[str, str]:
        """Try to apply the diff; on failure, ask the LLM for a corrected diff."""
        limit = max_retries if max_retries is not None else self._max_diff_retries
        attempts = 0
        last_error: str = ""
        current_plan, current_diff = plan, diff
        while True:
            try:
                await self._ws.apply_diff(repo, current_diff, task_id=task_id)
                return current_plan, current_diff
            except WorkspaceError as e:
                last_error = str(e)
                attempts += 1
                if attempts > limit:
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
                    system_prompt=system_prompt,
                )

    @staticmethod
    def _pick(overrides: RepoOverrides | None, attr: str, default: Any) -> Any:
        """Prefer an override value when it's set, otherwise fall back to default."""
        if overrides is None:
            return default
        value = getattr(overrides, attr, None)
        return value if value is not None else default

    @staticmethod
    async def resolve_pr_target_branch(
        github: _GitHubBranchProto,
        repo: str,
        *,
        preferred: str,
        fallback_to_default: bool,
    ) -> str:
        """Pick the branch a PR should target.

        If ``preferred`` exists on the remote, use it. Otherwise:
          * ``fallback_to_default=True``  → silently fall back to the default branch.
          * ``fallback_to_default=False`` → raise ``IssueExecutionError`` so the
            operator notices a misconfigured fleet manifest early.

        Skips the branch listing when ``preferred`` already is the default
        (common for Maxwell-Daemon itself merging directly to ``main``).
        """
        default = await github.get_default_branch(repo)
        if preferred == default:
            return preferred
        branches = await github.list_branches(repo)
        if preferred in branches:
            return preferred
        if not fallback_to_default:
            raise IssueExecutionError(
                f"configured pr_target_branch {preferred!r} not found on {repo!r} "
                "and fallback is disabled"
            )
        return default

    async def _refine_diff(
        self,
        *,
        issue_title: str,
        issue_body: str,
        model: str,
        previous_plan: str,
        previous_diff: str,
        error: str,
        system_prompt: str = _SYSTEM_PROMPT,
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
                Message(role=MessageRole.SYSTEM, content=system_prompt),
                Message(role=MessageRole.USER, content=prompt),
            ],
            model=model,
            temperature=0.2,
        )
        return self._parse_response(response.content)

    async def _draft_change(
        self,
        *,
        issue_title: str,
        issue_body: str,
        model: str,
        context: str = "",
        memory: str = "",
        labels: list[str] | None = None,
        system_prompt: str | None = None,
    ) -> tuple[str, str]:
        # Pick a specialised system prompt for this kind of issue. Falls back
        # to the default prompt when the classifier can't decide.
        kind = classify_issue(title=issue_title, body=issue_body, labels=labels or [])
        base_prompt = render_system_prompt(kind)
        # If a per-repo system prompt override was provided, it has already been
        # prepended (in _build_system_prompt); otherwise use the classified prompt.
        effective = system_prompt if system_prompt is not None else base_prompt

        prompt_parts = [f"Issue title: {issue_title}\n"]
        prompt_parts.append(f"Issue body:\n{issue_body or '(empty)'}\n")
        if memory:
            prompt_parts.append(f"\n## Memory\n\n{memory}\n")
        if context:
            prompt_parts.append(f"\n## Repository context\n\n{context}\n")
        prompt_parts.append("\nProduce the JSON plan now.")
        prompt = "\n".join(prompt_parts)
        response = await self._backend.complete(
            [
                Message(role=MessageRole.SYSTEM, content=effective),
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
        if content.startswith("```"):
            lines = content.splitlines()
            if len(lines) >= 2 and lines[-1].strip() == "```":
                # Strip the opening fence line and closing fence line
                content = "\n".join(lines[1:-1]).strip()
            else:
                # If there's no closing fence on its own line,
                # try stripping just the leading ```json and trailing ```
                first_newline = content.find('\n')
                if first_newline != -1:
                    content = content[first_newline+1:].strip()
                if content.endswith("```"):
                    content = content[:-3].strip()
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
    def _format_pr_body(
        *,
        issue_number: int,
        plan: str,
        applied: bool,
        test_result: TestResult | None = None,
    ) -> str:
        header_lines = [
            f"Closes #{issue_number}",
            "",
            f"> 🤖 Drafted by Maxwell-Daemon — {'code applied' if applied else 'plan only'}.",
        ]
        if test_result is not None:
            mark = "✅" if test_result.passed else "⚠️"
            verb = "passed" if test_result.passed else "failed"
            header_lines.append(
                f"> {mark} Tests {verb}: `{test_result.command}` "
                f"(rc={test_result.returncode}, {test_result.duration_seconds:.1f}s)"
            )
        header_lines.extend(["", ""])
        return "\n".join(header_lines) + plan
