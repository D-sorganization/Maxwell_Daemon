"""TestRunner — detect repo's test framework and run it safely."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from maxwell_daemon.gh.test_runner import (
    TestResult,
    TestRunner,
    TestRunnerError,
    detect_command,
)


class TestCommandDetection:
    def test_pytest_from_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"
        )
        assert detect_command(tmp_path) == ["python", "-m", "pytest"]

    def test_pytest_from_tests_dir(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        assert detect_command(tmp_path) == ["python", "-m", "pytest"]

    def test_npm_from_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"scripts":{"test":"jest"}}')
        assert detect_command(tmp_path) == ["npm", "test"]

    def test_package_json_without_test_script_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"scripts":{}}')
        assert detect_command(tmp_path) is None

    def test_go_test(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module x\n")
        assert detect_command(tmp_path) == ["go", "test", "./..."]

    def test_cargo_test(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[package]\n")
        assert detect_command(tmp_path) == ["cargo", "test"]

    def test_make_test(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("test:\n\techo ok\n")
        assert detect_command(tmp_path) == ["make", "test"]

    def test_returns_none_when_unknown(self, tmp_path: Path) -> None:
        assert detect_command(tmp_path) is None


class TestRunnerExecute:
    def test_passing_run(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()

        async def fake_runner(
            *argv: str, cwd: str | None = None, stdin: bytes | None = None
        ) -> tuple[int, bytes, bytes]:
            return 0, b"2 passed in 0.01s\n", b""

        runner = TestRunner(runner=fake_runner)
        result = asyncio.run(runner.detect_and_run(tmp_path))
        assert result.passed is True
        assert "passed" in result.output_tail
        assert result.returncode == 0

    def test_failing_run(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()

        async def fake_runner(
            *argv: str, cwd: str | None = None, stdin: bytes | None = None
        ) -> tuple[int, bytes, bytes]:
            return 1, b"", b"FAILED tests/test_x.py::t - AssertionError\n"

        runner = TestRunner(runner=fake_runner)
        result = asyncio.run(runner.detect_and_run(tmp_path))
        assert result.passed is False
        assert "FAILED" in result.output_tail

    def test_no_runner_detected_raises(self, tmp_path: Path) -> None:
        runner = TestRunner()
        with pytest.raises(TestRunnerError, match="could not detect"):
            asyncio.run(runner.detect_and_run(tmp_path))

    def test_explicit_command_overrides_detection(self, tmp_path: Path) -> None:
        async def fake_runner(
            *argv: str, cwd: str | None = None, stdin: bytes | None = None
        ) -> tuple[int, bytes, bytes]:
            assert argv == ("bash", "-c", "echo custom")
            return 0, b"ok\n", b""

        runner = TestRunner(runner=fake_runner)
        result = asyncio.run(runner.detect_and_run(tmp_path, command=["bash", "-c", "echo custom"]))
        assert result.passed is True
        assert result.command == "bash -c echo custom"

    def test_timeout_marks_failure(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()

        async def slow_runner(
            *argv: str, cwd: str | None = None, stdin: bytes | None = None
        ) -> tuple[int, bytes, bytes]:
            await asyncio.sleep(2.0)
            return 0, b"", b""

        runner = TestRunner(runner=slow_runner, default_timeout_seconds=0.05)
        result = asyncio.run(runner.detect_and_run(tmp_path))
        assert result.passed is False
        assert "timeout" in result.output_tail.lower()

    def test_tail_bounded(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        huge = b"line\n" * 100_000

        async def runny(
            *argv: str, cwd: str | None = None, stdin: bytes | None = None
        ) -> tuple[int, bytes, bytes]:
            return 0, huge, b""

        runner = TestRunner(runner=runny, tail_bytes=1024)
        result = asyncio.run(runner.detect_and_run(tmp_path))
        assert len(result.output_tail) <= 1100  # slack for "... truncated" marker


class TestResultDataclass:
    def test_fields(self) -> None:
        r = TestResult(
            passed=True,
            command="pytest",
            returncode=0,
            duration_seconds=1.5,
            output_tail="ok",
        )
        assert r.passed is True
