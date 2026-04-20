"""Detect + run a repo's test suite.

Used by the IssueExecutor to validate diffs before opening a PR. Test output is
captured, bounded, and returned so callers can feed failures back into an LLM
refinement loop.

Security considerations
-----------------------
- Subprocess is invoked with an argv list (never a shell string) except when
  the user explicitly provides a command like ``["bash", "-c", ...]`` — that's
  their decision.
- Timeouts are enforced to prevent a runaway or malicious test from DoSing the
  daemon.
- Output is tail-truncated so we don't blow out memory on a chatty test run.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "OnChunkCallback",
    "TestResult",
    "TestRunner",
    "TestRunnerError",
    "detect_command",
]

RunnerFn = Callable[..., Awaitable[tuple[int, bytes, bytes]]]
#: Called with (chunk_text, stream_name) for every line/chunk of test output.
OnChunkCallback = Callable[[str, str], Awaitable[None]]


class TestRunnerError(RuntimeError):
    """Raised when the test runner can't detect or run tests."""

    # Tell pytest not to try to collect this as a test class — the `Test` prefix
    # is meaningful here (it describes a test runner) not a pytest convention.
    __test__ = False


@dataclass(slots=True, frozen=True)
class TestResult:
    passed: bool
    command: str
    returncode: int
    duration_seconds: float
    output_tail: str

    __test__ = False


def detect_command(repo_path: Path) -> list[str] | None:
    """Infer a test command from repo-root markers.

    Order matters: we check more specific markers first (pyproject + pytest
    config) before falling back to directory heuristics.
    """
    if _has_pytest(repo_path):
        return ["python", "-m", "pytest"]

    if (pkg := repo_path / "package.json").is_file():
        try:
            data = json.loads(pkg.read_text())
        except json.JSONDecodeError:
            data = {}
        if isinstance(data.get("scripts"), dict) and "test" in data["scripts"]:
            return ["npm", "test"]

    if (repo_path / "go.mod").is_file():
        return ["go", "test", "./..."]

    if (repo_path / "Cargo.toml").is_file():
        return ["cargo", "test"]

    if (repo_path / "Makefile").is_file():
        text = (repo_path / "Makefile").read_text(errors="replace")
        if "test:" in text:
            return ["make", "test"]

    return None


def _has_pytest(repo_path: Path) -> bool:
    if (repo_path / "pytest.ini").is_file():
        return True
    if (repo_path / "tests").is_dir():
        return True
    pyproject = repo_path / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(errors="replace")
        if "[tool.pytest" in text or "pytest" in text:
            return True
    return False


async def _default_runner(
    *argv: str, cwd: str | None = None, stdin: bytes | None = None
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


class TestRunner:
    __test__ = False

    def __init__(
        self,
        *,
        runner: RunnerFn | None = None,
        default_timeout_seconds: float = 300.0,
        tail_bytes: int = 8192,
        on_chunk: OnChunkCallback | None = None,
    ) -> None:
        self._run = runner or _default_runner
        self._default_timeout = default_timeout_seconds
        self._tail_bytes = tail_bytes
        self._on_chunk = on_chunk

    async def detect_and_run(
        self,
        repo_path: Path,
        *,
        command: list[str] | None = None,
        timeout: float | None = None,
        on_chunk: OnChunkCallback | None = None,
    ) -> TestResult:
        cmd = command or detect_command(repo_path)
        if cmd is None:
            raise TestRunnerError(
                f"could not detect a test command in {repo_path}. "
                "Set repo.test_command in maxwell-daemon.yaml to override."
            )
        # Per-call callback takes precedence over the constructor-supplied one
        # so the executor can bind task-specific context (like task_id).
        callback = on_chunk or self._on_chunk
        return await self._run_with_timeout(
            cmd,
            cwd=repo_path,
            timeout=timeout or self._default_timeout,
            on_chunk=callback,
        )

    async def _run_with_timeout(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        on_chunk: OnChunkCallback | None = None,
    ) -> TestResult:
        start = time.monotonic()
        command_str = " ".join(argv)
        kwargs: dict[str, Any] = {"cwd": str(cwd)}
        if on_chunk is not None and _runner_accepts_on_chunk(self._run):
            kwargs["on_chunk"] = on_chunk
        try:
            rc, stdout, stderr = await asyncio.wait_for(self._run(*argv, **kwargs), timeout=timeout)
        except asyncio.TimeoutError:
            return TestResult(
                passed=False,
                command=command_str,
                returncode=-1,
                duration_seconds=time.monotonic() - start,
                output_tail=f"timeout after {timeout:.1f}s",
            )

        merged = stdout + stderr
        tail = merged[-self._tail_bytes :]
        if len(merged) > self._tail_bytes:
            tail = b"... truncated ...\n" + tail
        return TestResult(
            passed=rc == 0,
            command=command_str,
            returncode=rc,
            duration_seconds=time.monotonic() - start,
            output_tail=tail.decode(errors="replace"),
        )


def _runner_accepts_on_chunk(runner: RunnerFn) -> bool:
    try:
        sig = inspect.signature(runner)
    except (TypeError, ValueError):
        return False
    return "on_chunk" in sig.parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
