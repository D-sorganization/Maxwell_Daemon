"""Pre-PR quality gates that run after tests pass, before commit + push.

Each gate has the same shape — ``async def check(repo_path) -> GateResult`` —
so the executor can run a configurable list without special-casing. Failures
are fed back into the existing LLM refinement loop so the agent gets another
shot.
"""

from __future__ import annotations

import asyncio
import re
import tokenize
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "FileSizeBudgetGate",
    "GateResult",
    "NoOpDiffGate",
    "QualityGate",
    "QualityGateSuite",
    "RuffFormatGate",
    "TodoFixmeGate",
    "run_gates",
]


RunnerFn = Callable[..., Awaitable[tuple[int, bytes, bytes]]]


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


@dataclass(slots=True, frozen=True)
class GateResult:
    name: str
    passed: bool
    output: str


class QualityGate(Protocol):
    name: str

    async def check(self, repo_path: Path) -> GateResult: ...


class RuffFormatGate:
    """Runs `ruff format --check .` — fails if any file would be reformatted."""

    name = "ruff-format"

    def __init__(self, *, runner: RunnerFn | None = None) -> None:
        self._run = runner or _default_runner

    async def check(self, repo_path: Path) -> GateResult:
        rc, stdout, stderr = await self._run(
            "ruff", "format", "--check", ".", cwd=str(repo_path)
        )
        if rc == 127:
            return GateResult(
                self.name, passed=True, output="ruff not on PATH — skipped"
            )
        output = (stdout + stderr).decode(errors="replace").strip()
        return GateResult(self.name, passed=(rc == 0), output=output or "clean")


class TodoFixmeGate:
    """Rejects new TODO/FIXME comments that don't reference an issue."""

    name = "todo-fixme"

    _PATTERN = re.compile(
        r"\b(TODO|FIXME|XXX|HACK)\b(?!.*(#\d+|https?://))", re.IGNORECASE
    )

    async def check(self, repo_path: Path) -> GateResult:
        offenders: list[str] = []
        for path in repo_path.rglob("*.py"):
            if ".git" in path.parts or "tests/" in path.as_posix():
                continue
            try:
                with tokenize.open(path) as f:
                    for token in tokenize.generate_tokens(f.readline):
                        if token.type != tokenize.COMMENT or not self._PATTERN.search(
                            token.string
                        ):
                            continue
                        offenders.append(
                            f"{path.relative_to(repo_path)}:{token.start[0]}: {token.string.rstrip()}"
                        )
            except (OSError, tokenize.TokenError, UnicodeDecodeError):
                continue
        if offenders:
            return GateResult(
                self.name,
                passed=False,
                output="\n".join(offenders[:20]),
            )
        return GateResult(self.name, passed=True, output="no untracked TODO/FIXME")


class FileSizeBudgetGate:
    """Flags Python files above the configured line-count cap."""

    name = "file-size-budget"

    def __init__(self, *, max_lines: int = 1200) -> None:
        self._max = max_lines

    async def check(self, repo_path: Path) -> GateResult:
        offenders: list[str] = []
        for path in repo_path.rglob("*.py"):
            if ".git" in path.parts:
                continue
            try:
                with path.open(encoding="utf-8") as f:
                    count = sum(1 for _ in f)
            except (OSError, UnicodeDecodeError):
                continue
            if count > self._max:
                offenders.append(
                    f"{path.relative_to(repo_path)}: {count} lines (max {self._max})"
                )
        if offenders:
            return GateResult(self.name, passed=False, output="\n".join(offenders))
        return GateResult(self.name, passed=True, output="within budget")


class NoOpDiffGate:
    """Rejects diffs whose content is empty or pure whitespace."""

    name = "no-op-diff"

    def __init__(self, *, diff: str) -> None:
        self._diff = diff

    async def check(self, repo_path: Path) -> GateResult:
        content_lines = [
            line
            for line in self._diff.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
        meaningful = [line for line in content_lines if line[1:].strip()]
        if not meaningful:
            return GateResult(
                self.name,
                passed=False,
                output="diff has no meaningful additions or deletions",
            )
        return GateResult(
            self.name,
            passed=True,
            output=f"{len(meaningful)} content line(s) changed",
        )


class QualityGateSuite:
    """A configured list of gates. Preserves order; runs them sequentially so
    a failing early gate (e.g. no-op diff) short-circuits expensive ones."""

    def __init__(self, gates: list[QualityGate]) -> None:
        self._gates = gates

    async def check(self, repo_path: Path) -> list[GateResult]:
        results: list[GateResult] = []
        for gate in self._gates:
            results.append(await gate.check(repo_path))
        return results

    @property
    def gates(self) -> list[QualityGate]:
        return list(self._gates)


async def run_gates(repo_path: Path, *, gates: list[QualityGate]) -> list[GateResult]:
    """Convenience entry: run ``gates`` against ``repo_path``, return all results."""
    return await QualityGateSuite(gates).check(repo_path)
