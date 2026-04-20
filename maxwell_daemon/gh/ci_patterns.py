"""Infer a repo's CI contract from its checked-in configuration files.

The agent opens PRs that fail on round one when it doesn't know what the
repo's CI actually checks. This module walks a workspace and produces a
:class:`CIProfile` — a frozen snapshot of "what you must satisfy before
merge" — that downstream code renders into the system prompt.

Why a separate module rather than buried in ``ContextBuilder``? **LOD**:
each piece of evidence (pyproject.toml, mypy.ini, .pre-commit-config.yaml,
workflow files) is consulted by one detector method that only needs the
workspace path. ``ContextBuilder`` stays focused on git-level context and
composes this detector via a single call.

DbC: the detector constructor enforces ``workspace.is_dir()``; individual
detector methods are total — they return a clean default on any read
failure rather than raising, because a malformed config file shouldn't
abort the whole context build.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from maxwell_daemon.contracts import require

if sys.version_info >= (3, 11):  # pragma: no cover — import guarded by runtime version
    import tomllib
else:  # pragma: no cover — Python 3.10 fallback path
    import tomli as tomllib  # type: ignore[import-not-found]

__all__ = [
    "CIPatternDetector",
    "CIProfile",
    "detect_ci_profile",
]


# ── Regex constants ──────────────────────────────────────────────────────────

_COV_FAIL_UNDER_RE = re.compile(r"--cov-fail-under[ =]+(\d+(?:\.\d+)?)")
_MYPY_SECTION_RE = re.compile(r"^\[mypy\]", re.MULTILINE)
_MYPY_STRICT_RE = re.compile(r"^strict\s*=\s*(true|True)", re.MULTILINE)


# ── Data ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class CIProfile:
    """What a repo's CI expects a contributor to satisfy before merge.

    All fields default to "not detected" so an empty profile is a safe
    no-op — rendering yields an empty string, and callers treat the
    absence of a signal as "don't assert this in the system prompt."
    """

    uses_ruff: bool = False
    ruff_version: str | None = None
    uses_mypy: bool = False
    mypy_strict: bool = False
    uses_black: bool = False
    uses_pytest: bool = False
    coverage_floor: float | None = None
    has_precommit: bool = False
    precommit_hooks: tuple[str, ...] = field(default_factory=tuple)
    workflows: tuple[str, ...] = field(default_factory=tuple)

    def _has_any_signal(self) -> bool:
        return any(
            (
                self.uses_ruff,
                self.uses_mypy,
                self.uses_black,
                self.uses_pytest,
                self.coverage_floor is not None,
                self.has_precommit,
                self.workflows,
            )
        )

    def to_prompt(self) -> str:
        """Render the profile as a markdown checklist for the system prompt.

        Returns an empty string when nothing was detected so callers can
        drop the whole section unconditionally.
        """
        if not self._has_any_signal():
            return ""

        lines: list[str] = ["## CI requirements (must pass before merge)", ""]

        if self.uses_ruff:
            version = f" {self.ruff_version}" if self.ruff_version else ""
            lines.append(f"- **Linting:** ruff{version} — run `ruff check .` before committing")
            lines.append(
                "- **Formatting:** `ruff format --check .` must pass (run `ruff format .` to fix)"
            )

        if self.uses_black:
            lines.append("- **Formatting:** black — run `black .` before committing")

        if self.uses_mypy:
            strict_note = (
                " (strict mode — every function must have type hints)" if self.mypy_strict else ""
            )
            lines.append(f"- **Type checking:** mypy{strict_note} — must pass with zero errors")

        if self.uses_pytest:
            cov_note = ""
            if self.coverage_floor is not None:
                cov_note = f" with ≥{self.coverage_floor:g}% coverage enforced"
            lines.append(f"- **Tests:** pytest{cov_note}")

        if self.has_precommit:
            hooks = ", ".join(self.precommit_hooks) if self.precommit_hooks else "configured hooks"
            lines.append(f"- **Pre-commit:** {hooks} — run `pre-commit run --all-files` to verify")

        if self.workflows:
            listed = ", ".join(self.workflows)
            lines.append(f"- **GitHub Actions workflows:** {listed}")

        return "\n".join(lines)


# ── Detector ────────────────────────────────────────────────────────────────


class CIPatternDetector:
    """Produces a :class:`CIProfile` from a checked-in workspace.

    One instance per workspace; ``detect()`` is pure and idempotent. Read
    failures are swallowed into the appropriate "no" default — a malformed
    pyproject.toml doesn't break the whole context build.
    """

    def __init__(self, workspace: Path) -> None:
        require(
            workspace.is_dir(),
            f"CIPatternDetector: workspace {workspace} must be a directory",
        )
        self._root = workspace

    # ── Public API ───────────────────────────────────────────────────────────

    def detect(self) -> CIProfile:
        pyproject = self._read_pyproject()
        (
            uses_ruff,
            mypy_from_pyproject,
            mypy_strict_from_pyproject,
            pytest_from_pyproject,
            coverage_floor,
        ) = self._from_pyproject(pyproject)
        mypy_from_ini, mypy_strict_from_ini = self._from_mypy_ini()
        uses_black = self._uses_black(pyproject)
        has_precommit, hooks, ruff_version = self._from_precommit()
        workflows = self._workflows()

        return CIProfile(
            uses_ruff=uses_ruff,
            ruff_version=ruff_version,
            uses_mypy=mypy_from_pyproject or mypy_from_ini,
            mypy_strict=mypy_strict_from_pyproject or mypy_strict_from_ini,
            uses_black=uses_black,
            uses_pytest=pytest_from_pyproject,
            coverage_floor=coverage_floor,
            has_precommit=has_precommit,
            precommit_hooks=tuple(hooks),
            workflows=tuple(workflows),
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _read_pyproject(self) -> dict[str, Any]:
        """Parse pyproject.toml. Returns ``{}`` if missing or malformed."""
        path = self._root / "pyproject.toml"
        if not path.is_file():
            return {}
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _from_pyproject(
        self, pyproject: dict[str, Any]
    ) -> tuple[bool, bool, bool, bool, float | None]:
        """Pull ruff/mypy/pytest flags out of a parsed pyproject.toml.

        Returns ``(uses_ruff, uses_mypy, mypy_strict, uses_pytest, coverage_floor)``.
        """
        tool = pyproject.get("tool") if isinstance(pyproject, dict) else None
        if not isinstance(tool, dict):
            return False, False, False, False, None

        uses_ruff = "ruff" in tool
        mypy_cfg = tool.get("mypy")
        uses_mypy = isinstance(mypy_cfg, dict)
        mypy_strict = bool(isinstance(mypy_cfg, dict) and mypy_cfg.get("strict"))

        pytest_cfg = tool.get("pytest")
        uses_pytest = isinstance(pytest_cfg, dict)
        coverage_floor = self._extract_coverage_floor(pytest_cfg)

        return uses_ruff, uses_mypy, mypy_strict, uses_pytest, coverage_floor

    @staticmethod
    def _extract_coverage_floor(pytest_cfg: Any) -> float | None:
        if not isinstance(pytest_cfg, dict):
            return None
        ini_options = pytest_cfg.get("ini_options")
        if not isinstance(ini_options, dict):
            return None
        addopts = ini_options.get("addopts")
        if not isinstance(addopts, str):
            return None
        match = _COV_FAIL_UNDER_RE.search(addopts)
        if match is None:
            return None
        try:
            return float(match.group(1))
        except ValueError:  # pragma: no cover — regex guarantees numeric
            return None

    def _from_mypy_ini(self) -> tuple[bool, bool]:
        """Return ``(uses_mypy, mypy_strict)`` based on a standalone mypy.ini."""
        path = self._root / "mypy.ini"
        if not path.is_file():
            return False, False
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False, False
        if not _MYPY_SECTION_RE.search(text):
            return False, False
        return True, bool(_MYPY_STRICT_RE.search(text))

    def _uses_black(self, pyproject: dict[str, Any]) -> bool:
        tool = pyproject.get("tool") if isinstance(pyproject, dict) else None
        return isinstance(tool, dict) and "black" in tool

    def _from_precommit(self) -> tuple[bool, list[str], str | None]:
        """Return ``(has_precommit, hook_ids, ruff_version)``."""
        path = self._root / ".pre-commit-config.yaml"
        if not path.is_file():
            return False, [], None
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, yaml.YAMLError):
            return False, [], None
        if not isinstance(raw, dict):
            return True, [], None

        hooks: list[str] = []
        ruff_version: str | None = None
        for repo_entry in raw.get("repos", []) or []:
            if not isinstance(repo_entry, dict):
                continue
            repo_url = str(repo_entry.get("repo") or "")
            rev = repo_entry.get("rev")
            for hook in repo_entry.get("hooks", []) or []:
                if not isinstance(hook, dict):
                    continue
                hook_id = hook.get("id")
                if isinstance(hook_id, str):
                    hooks.append(hook_id)
            if ruff_version is None and "ruff-pre-commit" in repo_url and isinstance(rev, str):
                ruff_version = rev
        return True, hooks, ruff_version

    def _workflows(self) -> list[str]:
        """Return the basenames of ``.github/workflows/*.y{a,}ml``."""
        workflow_dir = self._root / ".github" / "workflows"
        if not workflow_dir.is_dir():
            return []
        names = [
            p.name
            for p in sorted(workflow_dir.iterdir())
            if p.is_file() and p.suffix in {".yml", ".yaml"}
        ]
        return names


# ── Convenience ─────────────────────────────────────────────────────────────


def detect_ci_profile(workspace: Path) -> CIProfile:
    """One-shot wrapper so callers don't need to manage a detector instance."""
    return CIPatternDetector(workspace).detect()
