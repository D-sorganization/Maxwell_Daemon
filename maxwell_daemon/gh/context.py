"""Build a bounded-size repo snapshot for the LLM.

The issue body alone tells the model *what* to do; the repo context tells it
*where* and *how*. We gather language, a pruned file tree, the README, a few
keyword-matched file snippets, and recent commit subjects, then render them
into a prompt section with strict character budgeting.

Orthogonal to the executor: the context builder only knows git (via an
injectable runner) and the local filesystem — no network, no LLM.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from maxwell_daemon.contracts import require
from maxwell_daemon.gh.ci_patterns import CIProfile, detect_ci_profile
from maxwell_daemon.memory import RepoMemoryStore

__all__ = ["ContextBuilder", "RepoContext", "detect_language"]

RunnerFn = Callable[..., Awaitable[tuple[int, bytes, bytes]]]


_LANGUAGE_MARKERS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("package.json", "javascript"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
    ("pom.xml", "java"),
    ("Gemfile", "ruby"),
)

_README_CANDIDATES: tuple[str, ...] = (
    "README.md",
    "README.rst",
    "README.txt",
    "README",
    "readme.md",
)

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_SKIP_WORDS = frozenset(
    {
        "the",
        "and",
        "but",
        "for",
        "with",
        "not",
        "this",
        "that",
        "from",
        "into",
        "fix",
        "bug",
        "bugs",
        "issue",
        "issues",
        "repro",
        "when",
        "add",
        "remove",
        "update",
        "make",
        "use",
        "why",
        "has",
        "have",
    }
)


def detect_language(repo_path: Path) -> str | None:
    """Return a canonical language name based on repo-root marker files."""
    for filename, language in _LANGUAGE_MARKERS:
        if (repo_path / filename).exists():
            return language
    return None


@dataclass(slots=True)
class RepoContext:
    language: str | None = None
    file_tree: str = ""
    readme: str = ""
    memory: str = ""
    relevant_files: dict[str, str] = field(default_factory=dict)
    recent_commits: list[str] = field(default_factory=list)
    #: CI contract inferred from workspace files — see ``maxwell_daemon.gh.ci_patterns``.
    #: ``None`` for backwards-compatible callers that build a RepoContext by hand.
    ci_profile: CIProfile | None = None

    def to_prompt(self, *, max_chars: int = 32_000) -> str:
        """Render the context as a markdown block, bounded to ``max_chars``.

        Each section takes a share of the budget; over-long sections get
        truncated with a ``... truncated ...`` marker so the LLM knows the
        section was cut rather than short.
        """
        # Split the budget: 15% file tree, 25% README, 15% repo memory, 30% snippets,
        # 10% commits, 5% CI.
        budget_tree = max(200, int(max_chars * 0.15))
        budget_readme = max(300, int(max_chars * 0.25))
        budget_memory = max(300, int(max_chars * 0.15))
        budget_snippets = max(500, int(max_chars * 0.30))
        budget_commits = max(200, int(max_chars * 0.10))
        budget_ci = max(400, int(max_chars * 0.05))

        parts: list[str] = []
        parts.append(f"Language: {self.language or 'unknown'}")

        if self.ci_profile is not None:
            ci_text = self.ci_profile.to_prompt()
            if ci_text:
                parts.append("\n" + _truncate(ci_text, budget_ci))

        if self.file_tree:
            parts.append("\n## File tree\n")
            parts.append(_truncate(self.file_tree, budget_tree))

        if self.readme:
            parts.append("\n## README\n")
            parts.append(_truncate(self.readme, budget_readme))

        if self.memory:
            parts.append("\n## Repo memory\n")
            parts.append(_truncate(self.memory, budget_memory))

        if self.relevant_files:
            parts.append("\n## Likely relevant files\n")
            per_file = budget_snippets // max(1, len(self.relevant_files))
            for path, snippet in self.relevant_files.items():
                parts.append(f"\n### {path}\n")
                parts.append(_truncate(snippet, per_file))

        if self.recent_commits:
            parts.append("\n## Recent commits\n")
            joined = "\n".join(f"- {c}" for c in self.recent_commits)
            parts.append(_truncate(joined, budget_commits))

        return "\n".join(parts)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = max(0, max_chars - len(" ... truncated ..."))
    return text[:cut] + "\n... truncated ..."


async def _default_git_runner(*argv: str, cwd: str | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


class ContextBuilder:
    def __init__(
        self,
        *,
        git_runner: RunnerFn | None = None,
        file_tree_limit: int = 200,
        readme_max_bytes: int = 4096,
        snippet_max_bytes: int = 2048,
        commit_count: int = 10,
    ) -> None:
        self._run = git_runner or _default_git_runner
        self._file_tree_limit = file_tree_limit
        self._readme_max_bytes = readme_max_bytes
        self._snippet_max_bytes = snippet_max_bytes
        self._commit_count = commit_count

    async def build(
        self,
        repo_path: Path,
        issue_body: str,
        *,
        repo_id: str | None = None,
        issue_title: str = "",
        issue_number: int | None = None,
    ) -> RepoContext:
        require(
            repo_path.is_dir(),
            f"ContextBuilder.build: repo_path {repo_path} must exist and be a directory",
        )
        language = detect_language(repo_path)
        file_tree = await self._file_tree(repo_path, limit=self._file_tree_limit)
        readme = self._read_readme(repo_path)
        relevant = await self._find_relevant_files(repo_path, issue_body, top_n=5)
        commits = await self._recent_commits(repo_path, limit=self._commit_count)
        memory = self._read_repo_memory(
            repo_path,
            repo_id=repo_id or repo_path.name,
            issue_title=issue_title,
            issue_body=issue_body,
            issue_number=issue_number,
        )
        # Detect CI contract synchronously — file-system only, fast.
        ci_profile = detect_ci_profile(repo_path)
        return RepoContext(
            language=language,
            file_tree=file_tree,
            readme=readme,
            memory=memory,
            relevant_files=relevant,
            recent_commits=commits,
            ci_profile=ci_profile,
        )

    async def _file_tree(self, repo_path: Path, *, limit: int) -> str:
        rc, out, _ = await self._run("git", "ls-files", cwd=str(repo_path))
        if rc != 0:
            return ""
        lines = out.decode(errors="replace").splitlines()
        if len(lines) <= limit:
            return "\n".join(lines)
        shown = lines[:limit]
        shown.append(f"... {len(lines) - limit} more files truncated ...")
        return "\n".join(shown)

    def _read_readme(self, repo_path: Path) -> str:
        for name in _README_CANDIDATES:
            path = repo_path / name
            if path.is_file():
                data = path.read_bytes()[: self._readme_max_bytes]
                text = data.decode(errors="replace")
                if len(path.read_bytes()) > self._readme_max_bytes:
                    text += "\n... truncated ..."
                return text
        return ""

    async def _find_relevant_files(
        self, repo_path: Path, issue_body: str, *, top_n: int
    ) -> dict[str, str]:
        keywords = self._extract_keywords(issue_body)
        if not keywords:
            return {}

        rc, out, _ = await self._run("git", "ls-files", cwd=str(repo_path))
        if rc != 0:
            return {}

        files = out.decode(errors="replace").splitlines()
        # Rank files by how many keywords appear in the path.
        scored: list[tuple[int, str]] = []
        for path in files:
            path_lower = path.lower()
            score = sum(1 for kw in keywords if kw in path_lower)
            if score:
                scored.append((score, path))
        scored.sort(reverse=True)

        out_files: dict[str, str] = {}
        for _, path in scored[:top_n]:
            fp = repo_path / path
            if fp.is_file():
                try:
                    content = fp.read_bytes()[: self._snippet_max_bytes]
                    out_files[path] = content.decode(errors="replace")
                except OSError:
                    continue
        return out_files

    async def _recent_commits(self, repo_path: Path, *, limit: int) -> list[str]:
        rc, out, _ = await self._run(
            "git",
            "log",
            "--oneline",
            "--no-merges",
            f"-{limit}",
            "--format=%s",
            cwd=str(repo_path),
        )
        if rc != 0:
            return []
        return [line for line in out.decode(errors="replace").splitlines() if line]

    def _read_repo_memory(
        self,
        repo_path: Path,
        *,
        repo_id: str,
        issue_title: str,
        issue_body: str,
        issue_number: int | None,
    ) -> str:
        store = RepoMemoryStore(repo_path)
        if not store.memory_dir.exists():
            return ""
        issue_number_text = f"{issue_number}" if issue_number is not None else None
        try:
            return store.render_snapshot(
                repo_id=repo_id,
                work_item_id=issue_number_text,
                max_items=6,
                token_budget=1_000,
                max_chars=4_000,
            )
        except Exception:
            return ""

    @staticmethod
    def _extract_keywords(body: str) -> list[str]:
        words = _WORD_RE.findall(body.lower())
        return [w for w in words if w not in _SKIP_WORDS]
