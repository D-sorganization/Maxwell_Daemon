"""``.maxwell/rules/*.md`` — frontmatter-guided rule auto-attachment.

Cursor-style rules: each ``.md`` file under ``.maxwell/rules/`` carries a
YAML frontmatter block declaring ``description``, ``globs``,
``always_apply``, ``priority``. The loader turns them into
:class:`Rule` objects; :func:`select_rules` picks the ones whose globs
match the files the agent is touching this turn; :func:`render_rules`
concatenates the selected rule bodies into a single prompt block.

Why this is better than one big CLAUDE.md:

* **Scoped activation.** ``tests/**/*.py`` rules only load when the
  agent is in that territory — tokens unused elsewhere.
* **Independent iteration.** A team member can add a rule without
  touching the central doc.
* **Budget-aware.** :func:`select_rules` honours a ``max_chars`` cap,
  dropping the lowest-priority rules until the budget fits.

DbC / LOD: the loader is pure (directory -> tuple of rules); selection
is pure (rules x touched files -> subset). No module here reaches into
the agent loop or the prompt assembler — those integrate via
:func:`render_rules` at the call site.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "Rule",
    "RuleLoadError",
    "load_rules",
    "render_rules",
    "select_rules",
]


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n?(?P<body>.*)\Z",
    re.DOTALL,
)


class RuleLoadError(ValueError):
    """Raised when a rule file is structurally invalid."""


@dataclass(slots=True, frozen=True)
class Rule:
    """One ``.md`` rule with its parsed frontmatter."""

    name: str
    description: str
    globs: tuple[str, ...]
    always_apply: bool
    priority: int
    body: str
    source: Path


def load_rules(directory: Path) -> tuple[Rule, ...]:
    """Load every ``*.md`` rule under ``directory``.

    Returns an empty tuple if ``directory`` doesn't exist. Raises
    :class:`RuleLoadError` on *any* structurally invalid rule — we'd
    rather fail loudly than silently misbehave, because "rule not
    applied" isn't a visible failure mode otherwise.

    Ordering: descending by ``priority``; ties broken by filename.
    """
    if not directory.is_dir():
        return ()
    rules: list[Rule] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".md":
            continue
        rules.append(_parse_rule(path))
    rules.sort(key=lambda r: (-r.priority, r.name))
    return tuple(rules)


def _parse_rule(path: Path) -> Rule:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise RuleLoadError(
            f"{path}: no YAML frontmatter block (expected --- ... --- at top)"
        )
    try:
        parsed: Any = yaml.safe_load(match.group("yaml")) or {}
    except yaml.YAMLError as e:
        raise RuleLoadError(f"{path}: YAML frontmatter is invalid: {e}") from e
    if not isinstance(parsed, dict):
        raise RuleLoadError(f"{path}: YAML frontmatter must be a mapping")

    description = str(parsed.get("description") or "")
    raw_globs = parsed.get("globs") or []
    if not isinstance(raw_globs, list):
        raise RuleLoadError(f"{path}: globs must be a list")
    globs = tuple(str(g) for g in raw_globs)
    raw_always_apply = parsed.get("always_apply", False)
    if not isinstance(raw_always_apply, bool):
        raise RuleLoadError(
            f"{path}: always_apply must be a boolean, got {type(raw_always_apply).__name__!r}"
        )
    always_apply = raw_always_apply
    raw_priority = parsed.get("priority", 0)
    try:
        priority = int(raw_priority)
    except (TypeError, ValueError) as exc:
        raise RuleLoadError(
            f"{path}: priority must be an integer, got {raw_priority!r}"
        ) from exc

    body = match.group("body").strip()
    return Rule(
        name=path.stem,
        description=description,
        globs=globs,
        always_apply=always_apply,
        priority=priority,
        body=body,
        source=path,
    )


def select_rules(
    rules: tuple[Rule, ...],
    *,
    touched: tuple[str, ...],
    max_chars: int | None = None,
) -> tuple[Rule, ...]:
    """Pick the subset of ``rules`` that apply to ``touched`` under ``max_chars``.

    A rule is eligible when either ``always_apply`` is true or any of
    its globs matches at least one path in ``touched``. Selected rules
    are emitted in descending priority order (same as ``load_rules``).

    When ``max_chars`` is set, we pack rules in priority order until
    the next rule would push the total body size over budget — that
    rule and all lower-priority rules are dropped.
    """
    eligible = sorted(
        (r for r in rules if _rule_matches(r, touched)),
        key=lambda r: (-r.priority, r.name),
    )
    if max_chars is None:
        return tuple(eligible)

    selected: list[Rule] = []
    used = 0
    for rule in eligible:
        if used + len(rule.body) > max_chars:
            continue
        selected.append(rule)
        used += len(rule.body)
    return tuple(selected)


def _rule_matches(rule: Rule, touched: tuple[str, ...]) -> bool:
    if rule.always_apply:
        return True
    if not rule.globs:
        return False
    for glob in rule.globs:
        for path in touched:
            if fnmatch.fnmatch(path, glob):
                return True
    return False


def render_rules(rules: tuple[Rule, ...]) -> str:
    """Concatenate selected rules into one markdown block.

    Each rule becomes ``## Rule: <name>\\n<description>\\n\\n<body>``.
    Empty input yields an empty string so callers can splice the
    result unconditionally.
    """
    if not rules:
        return ""
    sections: list[str] = []
    for rule in rules:
        parts = [f"## Rule: {rule.name}"]
        if rule.description:
            parts.append(rule.description)
        parts.append(rule.body)
        sections.append("\n".join(parts))
    return "\n\n".join(sections)
