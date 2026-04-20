"""`maxwell-daemon issue ...` subcommand coverage.

Complements test_batch_dispatch (which hits the REST layer) and fills in the
CLI-invocation paths that the daemon tests don't touch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from maxwell_daemon.cli.main import app
from maxwell_daemon.gh import Issue


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class _FakeGH:
    def __init__(
        self,
        *,
        create_url: str = "https://github.com/o/r/issues/7",
        issues: list[Issue] | None = None,
    ) -> None:
        self._create_url = create_url
        self._issues = issues or []

    async def create_issue(
        self, repo: str, *, title: str, body: str, labels: list[str] | None = None
    ) -> str:
        return self._create_url

    async def list_issues(self, repo: str, *, state: str = "open", limit: int = 50) -> list[Issue]:
        return self._issues


class TestIssueNew:
    def test_creates_issue(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        from maxwell_daemon import cli

        monkeypatch.setattr(cli.issues, "GitHubClient", lambda: _FakeGH())
        r = runner.invoke(app, ["issue", "new", "owner/repo", "Fix it", "--body", "bug"])
        assert r.exit_code == 0
        assert "issues/7" in r.stdout

    def test_dispatch_flag_calls_http(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maxwell_daemon import cli

        monkeypatch.setattr(cli.issues, "GitHubClient", lambda: _FakeGH())
        # Also avoid a real config load — monkey-patch load_config.
        monkeypatch.setattr(cli.issues, "load_config", lambda _: None)

        captured: list[str] = []

        def fake_post(url: str, **_: Any) -> object:
            captured.append(url)

            class _R:
                def raise_for_status(self) -> None: ...
                def json(self) -> dict[str, Any]:
                    return {"id": "abc", "status": "queued"}

            return _R()

        with patch.object(httpx, "post", fake_post):
            r = runner.invoke(
                app,
                ["issue", "new", "owner/repo", "X", "--body", "B", "--dispatch"],
            )
        assert r.exit_code == 0
        assert any("dispatch" in u for u in captured)


class TestIssueList:
    def test_empty(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        from maxwell_daemon import cli

        monkeypatch.setattr(cli.issues, "GitHubClient", lambda: _FakeGH(issues=[]))
        r = runner.invoke(app, ["issue", "list", "owner/repo"])
        assert r.exit_code == 0
        assert "No issues" in r.stdout

    def test_renders_table(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        from maxwell_daemon import cli

        monkeypatch.setattr(
            cli.issues,
            "GitHubClient",
            lambda: _FakeGH(
                issues=[
                    Issue(number=1, title="T", body="", state="OPEN", labels=["bug"], url="u"),
                ]
            ),
        )
        r = runner.invoke(app, ["issue", "list", "owner/repo"])
        assert r.exit_code == 0
        assert "T" in r.stdout


class TestIssueDispatchBatchFromRepo:
    def test_repo_mode_filters_by_label(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from maxwell_daemon import cli

        monkeypatch.setattr(
            cli.issues,
            "GitHubClient",
            lambda: _FakeGH(
                issues=[
                    Issue(number=1, title="a", body="", state="OPEN", labels=["triage"], url="u"),
                    Issue(number=2, title="b", body="", state="OPEN", labels=["other"], url="u"),
                ]
            ),
        )

        captured: list[dict[str, Any]] = []

        def fake_post(url: str, **kw: Any) -> object:
            captured.append(kw.get("json"))

            class _R:
                def raise_for_status(self) -> None: ...
                def json(self) -> dict[str, Any]:
                    return {"dispatched": 1, "failed": 0, "failures": []}

            return _R()

        with patch.object(httpx, "post", fake_post):
            r = runner.invoke(
                app,
                [
                    "issue",
                    "dispatch-batch",
                    "--repo",
                    "owner/repo",
                    "--label",
                    "triage",
                ],
            )
        assert r.exit_code == 0, r.stdout
        # Only the 'triage' issue should have been dispatched.
        assert captured == [{"items": [{"repo": "owner/repo", "number": 1, "mode": "plan"}]}]

    def test_requires_either_file_or_repo(self, runner: CliRunner) -> None:
        r = runner.invoke(app, ["issue", "dispatch-batch"])
        assert r.exit_code == 1
        assert "--from-file" in r.stdout or "--repo" in r.stdout

    def test_empty_match_prints_none_and_exits_ok(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from maxwell_daemon import cli

        monkeypatch.setattr(cli.issues, "GitHubClient", lambda: _FakeGH(issues=[]))
        r = runner.invoke(app, ["issue", "dispatch-batch", "--repo", "owner/repo"])
        assert r.exit_code == 0
        assert "No issues" in r.stdout


class TestBatchFileParser:
    def test_rejects_malformed_line(self, tmp_path: Path) -> None:
        import typer

        from maxwell_daemon.cli.issues import _parse_batch_file

        bad = tmp_path / "bad.txt"
        bad.write_text("definitely not parseable\n")
        with pytest.raises(typer.BadParameter):
            _parse_batch_file(bad, default_mode="plan")

    def test_skips_blank_and_commented_lines(self, tmp_path: Path) -> None:
        from maxwell_daemon.cli.issues import _parse_batch_file

        good = tmp_path / "g.txt"
        good.write_text("\n# a comment\nowner/repo#5\n")
        items = _parse_batch_file(good, default_mode="implement")
        assert items == [{"repo": "owner/repo", "number": 5, "mode": "implement"}]
