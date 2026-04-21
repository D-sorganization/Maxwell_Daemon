"""Tests for the rules system — ``.maxwell/rules/*.md`` with frontmatter globs.

Cursor-style rules: each ``.md`` file under ``.maxwell/rules/`` carries a
YAML frontmatter block declaring ``description``, ``globs``,
``always_apply``, ``priority``. Rules auto-attach to the agent's
context when the touched file set matches the globs (or when
``always_apply`` is true).

Strictly better than a single ``CLAUDE.md`` because:
  * Rules activate only when their context matters (cheap prompt).
  * Per-file-type guidance goes in its own rule.
  * Priority orders overlapping rules deterministically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.rules import (
    Rule,
    RuleLoadError,
    load_rules,
    select_rules,
)


def _write_rule(
    rules_dir: Path,
    name: str,
    *,
    description: str = "",
    globs: list[str] | None = None,
    always_apply: bool = False,
    priority: int = 0,
    body: str = "rule body",
) -> Path:
    rules_dir.mkdir(parents=True, exist_ok=True)
    path = rules_dir / name
    lines = ["---", f"description: {description}"]
    if globs:
        glob_list = ", ".join(f'"{g}"' for g in globs)
        lines.append(f"globs: [{glob_list}]")
    lines.append(f"always_apply: {str(always_apply).lower()}")
    lines.append(f"priority: {priority}")
    lines.append("---")
    lines.append(body)
    path.write_text("\n".join(lines) + "\n")
    return path


# ── Shape ────────────────────────────────────────────────────────────────────


class TestRuleShape:
    def test_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        r = Rule(
            name="x",
            description="",
            globs=(),
            always_apply=False,
            priority=0,
            body="",
            source=Path("/x"),
        )
        with pytest.raises(FrozenInstanceError):
            r.priority = 10  # type: ignore[misc]


# ── Loading ────────────────────────────────────────────────────────────────


class TestLoadRules:
    def test_empty_directory_returns_empty(self, tmp_path: Path) -> None:
        assert load_rules(tmp_path / "missing") == ()

    def test_parses_frontmatter_and_body(self, tmp_path: Path) -> None:
        _write_rule(
            tmp_path / "rules",
            "test-style.md",
            description="How we write tests",
            globs=["tests/**/*.py"],
            always_apply=False,
            priority=5,
            body="Use pytest parametrize for combinations.",
        )
        rules = load_rules(tmp_path / "rules")
        assert len(rules) == 1
        r = rules[0]
        assert r.name == "test-style"
        assert r.description == "How we write tests"
        assert r.globs == ("tests/**/*.py",)
        assert r.always_apply is False
        assert r.priority == 5
        assert "pytest parametrize" in r.body

    def test_sorted_by_priority_descending(self, tmp_path: Path) -> None:
        rd = tmp_path / "rules"
        _write_rule(rd, "low.md", priority=1)
        _write_rule(rd, "mid.md", priority=5)
        _write_rule(rd, "high.md", priority=10)
        rules = load_rules(rd)
        assert [r.name for r in rules] == ["high", "mid", "low"]

    def test_missing_frontmatter_raises(self, tmp_path: Path) -> None:
        rd = tmp_path / "rules"
        rd.mkdir()
        (rd / "bad.md").write_text("just some text, no frontmatter\n")
        with pytest.raises(RuleLoadError, match="frontmatter"):
            load_rules(rd)

    def test_malformed_yaml_frontmatter_raises(self, tmp_path: Path) -> None:
        rd = tmp_path / "rules"
        rd.mkdir()
        (rd / "bad.md").write_text("---\nnot: [valid\n---\nbody\n")
        with pytest.raises(RuleLoadError, match="YAML"):
            load_rules(rd)

    def test_non_md_files_ignored(self, tmp_path: Path) -> None:
        rd = tmp_path / "rules"
        rd.mkdir()
        (rd / "readme.txt").write_text("not a rule\n")
        _write_rule(rd, "real.md")
        rules = load_rules(rd)
        assert [r.name for r in rules] == ["real"]

    def test_default_values_when_frontmatter_fields_missing(
        self, tmp_path: Path
    ) -> None:
        rd = tmp_path / "rules"
        rd.mkdir()
        (rd / "minimal.md").write_text("---\ndescription: minimal\n---\nbody\n")
        rules = load_rules(rd)
        assert rules[0].globs == ()
        assert rules[0].always_apply is False
        assert rules[0].priority == 0


# ── Selection / matching ────────────────────────────────────────────────────


class TestSelectRules:
    def test_always_apply_rule_always_selected(self, tmp_path: Path) -> None:
        always = Rule(
            name="a",
            description="",
            globs=(),
            always_apply=True,
            priority=0,
            body="always",
            source=tmp_path / "a.md",
        )
        selected = select_rules((always,), touched=())
        assert selected == (always,)

    def test_glob_matched_rule_selected(self, tmp_path: Path) -> None:
        rule = Rule(
            name="py",
            description="",
            globs=("**/*.py",),
            always_apply=False,
            priority=0,
            body="",
            source=tmp_path / "py.md",
        )
        selected = select_rules((rule,), touched=("src/foo.py",))
        assert selected == (rule,)

    def test_unmatched_rule_excluded(self, tmp_path: Path) -> None:
        rule = Rule(
            name="py",
            description="",
            globs=("**/*.py",),
            always_apply=False,
            priority=0,
            body="",
            source=tmp_path / "py.md",
        )
        selected = select_rules((rule,), touched=("README.md", "docs/x.rst"))
        assert selected == ()

    def test_any_matching_glob_selects(self, tmp_path: Path) -> None:
        rule = Rule(
            name="multi",
            description="",
            globs=("**/*.py", "tests/**"),
            always_apply=False,
            priority=0,
            body="",
            source=tmp_path / "x.md",
        )
        selected = select_rules((rule,), touched=("tests/unit/test_x.md",))
        assert selected == (rule,)

    def test_priority_order_preserved_among_selected(self, tmp_path: Path) -> None:
        hi = Rule(
            name="hi",
            description="",
            globs=(),
            always_apply=True,
            priority=10,
            body="",
            source=tmp_path / "hi.md",
        )
        lo = Rule(
            name="lo",
            description="",
            globs=(),
            always_apply=True,
            priority=1,
            body="",
            source=tmp_path / "lo.md",
        )
        selected = select_rules((lo, hi), touched=())
        assert [r.name for r in selected] == ["hi", "lo"]

    def test_budget_caps_included_rules(self, tmp_path: Path) -> None:
        """If the composed rule body would exceed ``max_chars``, drop the
        lowest-priority rules until it fits."""
        big = Rule(
            name="big",
            description="",
            globs=(),
            always_apply=True,
            priority=1,
            body="x" * 2000,
            source=tmp_path / "big.md",
        )
        small = Rule(
            name="small",
            description="",
            globs=(),
            always_apply=True,
            priority=10,
            body="small body",
            source=tmp_path / "small.md",
        )
        selected = select_rules((big, small), touched=(), max_chars=200)
        names = [r.name for r in selected]
        # High-priority 'small' kept; low-priority 'big' dropped.
        assert "small" in names
        assert "big" not in names

    def test_combined_rule_body_never_exceeds_budget(self, tmp_path: Path) -> None:
        one = Rule(
            name="one",
            description="",
            globs=(),
            always_apply=True,
            priority=1,
            body="a" * 100,
            source=tmp_path / "one.md",
        )
        two = Rule(
            name="two",
            description="",
            globs=(),
            always_apply=True,
            priority=2,
            body="b" * 100,
            source=tmp_path / "two.md",
        )
        selected = select_rules((one, two), touched=(), max_chars=150)
        total_body = sum(len(r.body) for r in selected)
        assert total_body <= 150


# ── Rendering ──────────────────────────────────────────────────────────────


class TestRenderRules:
    def test_renders_as_markdown_sections(self, tmp_path: Path) -> None:
        from maxwell_daemon.rules import render_rules

        rules = (
            Rule(
                name="style",
                description="Code style rules",
                globs=(),
                always_apply=True,
                priority=5,
                body="Use ruff format.",
                source=tmp_path / "style.md",
            ),
        )
        out = render_rules(rules)
        assert "## Rule: style" in out
        assert "Use ruff format." in out
        assert "Code style rules" in out

    def test_empty_rules_renders_empty_string(self) -> None:
        from maxwell_daemon.rules import render_rules

        assert render_rules(()) == ""
