from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.checks import CheckLoadError, load_check, load_checks, select_checks


def _write_check(path: Path, *, body: str = "Check body.") -> None:
    path.write_text(
        f"""
---
id: {path.stem}
name: {path.stem.replace("-", " ").title()}
severity: required
applies_to:
  globs:
    - "**/*.py"
trigger:
  events: [pull_request, task_completed]
model_tier: moderate
---

{body}
""".lstrip(),
        encoding="utf-8",
    )


def test_loads_valid_check_and_matches_paths(tmp_path: Path) -> None:
    check_path = tmp_path / "scope-drift.md"
    _write_check(check_path)

    check = load_check(check_path)

    assert check.id == "scope-drift"
    assert check.name == "Scope Drift"
    assert check.severity.value == "required"
    assert check.model_tier.value == "moderate"
    assert check.applies_to_paths(("app.py",))
    assert check.applies_to_paths(("src/app.py",))
    assert check.triggers_on("pull_request")


def test_load_checks_is_deterministic_and_ignores_non_md_files(tmp_path: Path) -> None:
    _write_check(tmp_path / "b-check.md")
    _write_check(tmp_path / "a-check.md")
    (tmp_path / "ignore.txt").write_text("not a check", encoding="utf-8")

    checks = load_checks(tmp_path)

    assert [check.source.name for check in checks] == ["a-check.md", "b-check.md"]


def test_select_checks_filters_by_path(tmp_path: Path) -> None:
    py_check = tmp_path / "py-check.md"
    py_check.write_text(
        """
---
id: py-check
name: Python Check
severity: advisory
applies_to:
  globs:
    - "**/*.py"
trigger:
  events: [pull_request]
model_tier: simple
---

Python prompt body.
""".lstrip(),
        encoding="utf-8",
    )
    md_check = tmp_path / "docs-check.md"
    md_check.write_text(
        """
---
id: docs-check
name: Docs Check
severity: blocking
applies_to:
  globs:
    - "docs/**/*.md"
trigger:
  events: [task_completed]
model_tier: complex
---

Docs prompt body.
""".lstrip(),
        encoding="utf-8",
    )

    checks = load_checks(tmp_path)

    selected = select_checks(checks, touched_paths=("src/app.py", "README.md"))

    assert [check.id for check in selected] == ["py-check"]


def test_rejects_malformed_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text("just text", encoding="utf-8")

    with pytest.raises(CheckLoadError, match="no YAML frontmatter"):
        load_check(path)


def test_rejects_duplicate_ids(tmp_path: Path) -> None:
    _write_check(tmp_path / "first.md")
    _write_check(tmp_path / "second.md")
    (tmp_path / "second.md").write_text(
        """
---
id: first
name: Duplicate
severity: required
applies_to:
  globs:
    - "**/*.py"
trigger:
  events: [pull_request]
model_tier: moderate
---

Duplicate body.
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(CheckLoadError, match="duplicate check id"):
        load_checks(tmp_path)
