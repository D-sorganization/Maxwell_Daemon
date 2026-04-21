"""Pre-PR quality gates — check ruff, file size, TODO/FIXME, no-op diffs."""

from __future__ import annotations

import asyncio
from pathlib import Path

from maxwell_daemon.gh.quality_gates import (
    FileSizeBudgetGate,
    GateResult,
    NoOpDiffGate,
    QualityGateSuite,
    RuffFormatGate,
    TodoFixmeGate,
    run_gates,
)


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._responses: dict[tuple[str, ...], tuple[int, bytes, bytes]] = {}

    def respond(self, *argv: str, rc: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self._responses[argv] = (rc, stdout, stderr)

    async def __call__(
        self, *argv: str, cwd: str | None = None, stdin: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        self.calls.append(argv)
        return self._responses.get(argv, (0, b"", b""))


class TestGateResult:
    def test_passed_means_ok(self) -> None:
        r = GateResult(name="x", passed=True, output="fine")
        assert bool(r.passed)


class TestRuffFormatGate:
    def test_passes_when_format_clean(self, tmp_path: Path) -> None:
        runner = _RecordingRunner()
        runner.respond("ruff", "format", "--check", ".", rc=0)
        gate = RuffFormatGate(runner=runner)
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed is True

    def test_fails_when_format_mismatches(self, tmp_path: Path) -> None:
        runner = _RecordingRunner()
        runner.respond(
            "ruff",
            "format",
            "--check",
            ".",
            rc=1,
            stdout=b"Would reformat: x.py\n",
        )
        gate = RuffFormatGate(runner=runner)
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed is False
        assert "Would reformat" in r.output

    def test_skipped_when_ruff_missing(self, tmp_path: Path) -> None:
        runner = _RecordingRunner()
        runner.respond("ruff", "format", "--check", ".", rc=127)
        gate = RuffFormatGate(runner=runner)
        r = asyncio.run(gate.check(tmp_path))
        # rc=127 (command not found) → gate is skipped, not failed.
        assert r.passed is True
        assert "skipped" in r.output.lower()


class TestTodoFixmeGate:
    def test_passes_when_repo_clean(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("print('ok')\n")
        gate = TodoFixmeGate()
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed is True

    def test_flags_untracked_todo(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("# TODO: fix this\n")
        gate = TodoFixmeGate()
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed is False
        assert "TODO" in r.output

    def test_ignores_todo_in_string_literals(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text('PATTERN = "TODO|FIXME"\n')
        gate = TodoFixmeGate()
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed is True

    def test_allows_todo_with_issue_ref(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("# TODO #123: fix this\n")
        gate = TodoFixmeGate()
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed is True


class TestFileSizeBudgetGate:
    def test_passes_when_under_budget(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("\n" * 100)
        gate = FileSizeBudgetGate(max_lines=1200)
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed is True

    def test_fails_when_file_exceeds(self, tmp_path: Path) -> None:
        (tmp_path / "big.py").write_text("\n" * 2000)
        gate = FileSizeBudgetGate(max_lines=1200)
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed is False
        assert "big.py" in r.output


class TestNoOpDiffGate:
    def test_passes_when_diff_has_content(self, tmp_path: Path) -> None:
        gate = NoOpDiffGate(diff="diff --git a/x b/x\n+new line\n")
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed is True

    def test_fails_when_diff_empty(self, tmp_path: Path) -> None:
        gate = NoOpDiffGate(diff="")
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed is False

    def test_fails_when_diff_pure_whitespace(self, tmp_path: Path) -> None:
        gate = NoOpDiffGate(diff="diff --git a/x b/x\n   \n")
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed is False


class TestQualityGateSuite:
    def test_runs_all_gates(self, tmp_path: Path) -> None:
        runner = _RecordingRunner()
        runner.respond("ruff", "format", "--check", ".", rc=0)
        suite = QualityGateSuite([RuffFormatGate(runner=runner), TodoFixmeGate()])
        results = asyncio.run(suite.check(tmp_path))
        assert len(results) == 2
        assert all(r.passed for r in results)

    def test_all_passed_is_true_when_all_pass(self, tmp_path: Path) -> None:
        suite = QualityGateSuite([NoOpDiffGate(diff="diff --git a/x b/x\n+new\n")])
        results = asyncio.run(suite.check(tmp_path))
        assert all(r.passed for r in results)


class TestRunGates:
    def test_convenience_entry(self, tmp_path: Path) -> None:
        runner = _RecordingRunner()
        runner.respond("ruff", "format", "--check", ".", rc=0)
        results = asyncio.run(
            run_gates(
                tmp_path,
                gates=[
                    RuffFormatGate(runner=runner),
                    NoOpDiffGate(diff="diff --git a/x b/x\n+real change\n"),
                ],
            )
        )
        assert len(results) == 2
        assert all(r.passed for r in results)


class TestTodoFixmeGateEdgeCases:
    def test_skips_files_in_tests_subdir(self, tmp_path: Path) -> None:
        """Files under tests/ are excluded from the TODO scan."""
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_something.py").write_text("# TODO: improve this test\n")
        gate = TodoFixmeGate()
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed is True

    def test_handles_tokenize_error_gracefully(self, tmp_path: Path) -> None:
        """Files that raise TokenError during tokenization are silently skipped."""
        # Write a file that looks like Python but will cause tokenize errors
        (tmp_path / "bad.py").write_text("'''unclosed string\n# TODO no issue\n")
        gate = TodoFixmeGate()
        # Should not raise — the except clause swallows the error
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed in (True, False)  # may or may not flag it, but must not crash


class TestFileSizeBudgetGateEdgeCases:
    def test_skips_files_in_git_dir(self, tmp_path: Path) -> None:
        """Python files inside .git/ directories are skipped."""
        git_dir = tmp_path / ".git" / "hooks"
        git_dir.mkdir(parents=True)
        (git_dir / "pre-commit.py").write_text("\n" * 2000)
        gate = FileSizeBudgetGate(max_lines=1200)
        r = asyncio.run(gate.check(tmp_path))
        assert r.passed is True
