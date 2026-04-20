"""RepoContext builder — language detection, file tree, README, relevant files."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from maxwell_daemon.gh.context import ContextBuilder, RepoContext, detect_language


class TestLanguageDetection:
    def test_python(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert detect_language(tmp_path) == "python"

    def test_node(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        assert detect_language(tmp_path) == "javascript"

    def test_go(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module x\n")
        assert detect_language(tmp_path) == "go"

    def test_rust(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[package]\n")
        assert detect_language(tmp_path) == "rust"

    def test_unknown(self, tmp_path: Path) -> None:
        assert detect_language(tmp_path) is None


class TestFileTree:
    def test_tree_lists_tracked_files(self, tmp_path: Path) -> None:
        async def fake_git(*argv: str, cwd: str | None = None) -> tuple[int, bytes, bytes]:
            assert argv[:2] == ("git", "ls-files")
            return 0, b"a.py\nb.py\nsub/c.py\n", b""

        builder = ContextBuilder(git_runner=fake_git)
        tree = asyncio.run(builder._file_tree(tmp_path, limit=100))
        assert "a.py" in tree and "sub/c.py" in tree

    def test_tree_respects_limit(self, tmp_path: Path) -> None:
        files = b"\n".join(f"file{i}.py".encode() for i in range(500)) + b"\n"

        async def fake_git(*argv: str, cwd: str | None = None) -> tuple[int, bytes, bytes]:
            return 0, files, b""

        builder = ContextBuilder(git_runner=fake_git)
        tree = asyncio.run(builder._file_tree(tmp_path, limit=50))
        # Should be truncated with a marker line.
        assert tree.count("\n") <= 60
        assert "truncated" in tree.lower()


class TestReadme:
    def test_includes_readme(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# Project\n\nHello world.\n")
        builder = ContextBuilder()
        readme = builder._read_readme(tmp_path)
        assert "Hello world" in readme

    def test_truncates_long_readme(self, tmp_path: Path) -> None:
        big = "line\n" * 10_000
        (tmp_path / "README.md").write_text(big)
        builder = ContextBuilder(readme_max_bytes=1024)
        readme = builder._read_readme(tmp_path)
        assert len(readme) <= 1200  # allow some slack for the truncation marker

    def test_returns_empty_when_absent(self, tmp_path: Path) -> None:
        assert ContextBuilder()._read_readme(tmp_path) == ""

    def test_picks_first_available_readme_variant(self, tmp_path: Path) -> None:
        (tmp_path / "README.rst").write_text("RST readme content")
        assert "RST readme" in ContextBuilder()._read_readme(tmp_path)


class TestRelevantFiles:
    def test_keyword_match_by_filename(self, tmp_path: Path) -> None:
        (tmp_path / "parser.py").write_text("def parse(): pass\n")
        (tmp_path / "unrelated.py").write_text("def other(): pass\n")

        async def fake_git(*argv: str, cwd: str | None = None) -> tuple[int, bytes, bytes]:
            return 0, b"parser.py\nunrelated.py\n", b""

        builder = ContextBuilder(git_runner=fake_git)
        hits = asyncio.run(builder._find_relevant_files(tmp_path, "fix the parser output", top_n=5))
        assert "parser.py" in hits
        # unrelated.py shouldn't surface — no keyword match.
        assert "unrelated.py" not in hits or hits["parser.py"]

    def test_snippet_is_bounded(self, tmp_path: Path) -> None:
        big_content = "x = 1\n" * 5000
        (tmp_path / "big.py").write_text(big_content)

        async def fake_git(*argv: str, cwd: str | None = None) -> tuple[int, bytes, bytes]:
            return 0, b"big.py\n", b""

        builder = ContextBuilder(git_runner=fake_git, snippet_max_bytes=200)
        hits = asyncio.run(builder._find_relevant_files(tmp_path, "big", top_n=5))
        assert len(hits["big.py"]) <= 250


class TestBuild:
    def test_assembles_full_context(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "README.md").write_text("# X\n")
        (tmp_path / "parser.py").write_text("def parse(): pass\n")

        async def fake_git(*argv: str, cwd: str | None = None) -> tuple[int, bytes, bytes]:
            if argv[1] == "ls-files":
                return 0, b"parser.py\npyproject.toml\nREADME.md\n", b""
            if argv[1] == "log":
                return 0, b"fix parser\nadd readme\n", b""
            return 0, b"", b""

        builder = ContextBuilder(git_runner=fake_git)
        ctx = asyncio.run(builder.build(tmp_path, issue_body="fix the parser"))

        assert ctx.language == "python"
        assert "parser.py" in ctx.file_tree
        assert "X" in ctx.readme
        assert "parser.py" in ctx.relevant_files
        assert len(ctx.recent_commits) == 2


class TestPromptRendering:
    def test_prompt_has_all_sections(self) -> None:
        ctx = RepoContext(
            language="python",
            file_tree="a.py\nb.py\n",
            readme="# Project\n",
            relevant_files={"a.py": "def a(): pass\n"},
            recent_commits=["add a", "fix b"],
        )
        prompt = ctx.to_prompt(max_chars=10_000)
        assert "Language: python" in prompt
        assert "a.py" in prompt
        assert "Project" in prompt
        assert "add a" in prompt

    def test_prompt_respects_size_budget(self) -> None:
        big = textwrap.dedent("line\n" * 10_000)
        ctx = RepoContext(
            language="python",
            file_tree=big,
            readme=big,
            relevant_files={"a.py": big},
            recent_commits=[big],
        )
        prompt = ctx.to_prompt(max_chars=2000)
        assert len(prompt) <= 2500  # small slack for section headers


class TestContractViolation:
    def test_build_rejects_nonexistent_path(self, tmp_path: Path) -> None:
        from maxwell_daemon.contracts import PreconditionError

        builder = ContextBuilder()
        with pytest.raises(PreconditionError):
            asyncio.run(builder.build(tmp_path / "missing", issue_body="x"))
