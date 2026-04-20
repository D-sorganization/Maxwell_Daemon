"""Tests for ``maxwell-daemon session ...`` CLI commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from maxwell_daemon.cli.session import session_app
from maxwell_daemon.session import SessionLog, UserMessage


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestSessionList:
    def test_empty_directory(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(session_app, ["list", str(tmp_path)])
        assert result.exit_code == 0
        assert "No sessions" in result.output

    def test_lists_sessions(self, runner: CliRunner, tmp_path: Path) -> None:
        log = SessionLog(session_id="alpha", directory=tmp_path)
        log.append(UserMessage(session_id="alpha", seq=0, content="task"))
        result = runner.invoke(session_app, ["list", str(tmp_path)])
        assert result.exit_code == 0
        assert "alpha" in result.output
        # Count column shows at least 1 event.
        assert "1" in result.output


class TestSessionReplay:
    def test_replay_prints_transcript(self, runner: CliRunner, tmp_path: Path) -> None:
        log = SessionLog(session_id="alpha", directory=tmp_path)
        log.append(UserMessage(session_id="alpha", seq=0, content="hello there"))
        result = runner.invoke(session_app, ["replay", "alpha", "--directory", str(tmp_path)])
        assert result.exit_code == 0
        assert "hello there" in result.output

    def test_missing_session_exits_nonzero(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(session_app, ["replay", "nonexistent", "--directory", str(tmp_path)])
        assert result.exit_code == 1
