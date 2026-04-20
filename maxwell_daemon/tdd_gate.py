"""Red-green-refactor enforcement — the TDD discipline, made load-bearing.

The agent is required to:

  1. Add a *failing* test that captures the desired new behaviour.
  2. Implement the change until the test passes.
  3. Leave behind an audit trail so the reviewer can verify the test
     really was failing before the change.

Every non-trivial change the agent opens as a PR must pass through
:meth:`TddGate.verify_red_green`. If the test the agent wrote passes
*before* any implementation, the test was a no-op and the gate raises
:class:`TddViolationError`.

LOD: the test runner is a plain async callable (see :class:`RunnerFn`),
injected. The gate doesn't know pytest; it just asks "did the test pass?".
DbC: constructor enforces the workspace is a directory; the runner must
return a :class:`RunOutcome` (total) — never raise on a test failure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from maxwell_daemon.contracts import require

__all__ = [
    "RedGreenResult",
    "RunOutcome",
    "RunnerFn",
    "TddGate",
    "TddViolationError",
]


@dataclass(slots=True, frozen=True)
class RunOutcome:
    """The outcome of one pytest (or equivalent) invocation."""

    passed: bool
    returncode: int
    output: str
    duration_seconds: float = 0.0


@dataclass(slots=True, frozen=True)
class RedGreenResult:
    """Record of one red -> implement -> green cycle."""

    red_ran: bool
    red_failed: bool
    green_ran: bool
    green_passed: bool
    honest: bool
    detail: str = ""

    def to_pr_body_note(self) -> str:
        """One-line note for the PR body so reviewers can verify the cycle."""
        red = "failing" if self.red_failed else "passing-unexpectedly"
        green = "passing" if self.green_passed else "still-failing"
        return f"TDD gate: red ({red}) -> implement -> green ({green}); honest={self.honest}"


class TddViolationError(RuntimeError):
    """Raised when the new test passes before the implementation lands.

    Means the test doesn't actually cover the change — either it was a
    no-op, or the implementation already existed and the test-first
    discipline was skipped.
    """


RunnerFn = Callable[..., Awaitable[RunOutcome]]


class TddGate:
    """Runs the test twice (before + after the implementation) and audits the delta."""

    def __init__(
        self,
        *,
        workspace: Path,
        test_runner: RunnerFn,
    ) -> None:
        require(
            workspace.is_dir(),
            f"TddGate: workspace {workspace} must be a directory",
        )
        self._workspace = workspace
        self._run_tests = test_runner

    async def verify_red_green(
        self,
        *,
        test_paths: tuple[str, ...],
        implement: Callable[[], Awaitable[None]],
    ) -> RedGreenResult:
        """Run the TDD cycle.

        1. Run the tests — they MUST fail (RED). If they pass, the test
           wasn't covering anything; raise :class:`TddViolationError`.
        2. Call ``implement`` once.
        3. Run the same tests — they should now pass (GREEN). Returns the
           result either way; a non-raising failure lets the caller decide
           whether to iterate.
        """
        red = await self._run_tests(workspace=self._workspace, test_paths=test_paths)
        if red.passed:
            raise TddViolationError(
                "test-first violation: the new test(s) passed before the "
                "implementation ran. A TDD test must fail (RED) first so we "
                "know it covers the change.\n"
                f"Tests: {list(test_paths)}\nOutput:\n{red.output[:2000]}"
            )

        await implement()

        green = await self._run_tests(workspace=self._workspace, test_paths=test_paths)
        detail = "" if green.passed else green.output[-2000:]
        return RedGreenResult(
            red_ran=True,
            red_failed=not red.passed,
            green_ran=True,
            green_passed=green.passed,
            honest=not red.passed,
            detail=detail,
        )
