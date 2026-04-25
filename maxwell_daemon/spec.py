"""Gherkin / BDD spec ingestion and pytest-bdd scaffold generator.

The spec-driven flow:

  1. User writes a ``.feature`` file under ``.maxwell/specs/``.
  2. This module parses it into a :class:`Specification`.
  3. :func:`render_pytest_bdd_scaffold` emits a pytest-bdd skeleton —
     one ``@given``/``@when``/``@then`` stub per unique step text.
  4. The agent fills in step definitions + the production code they drive.
  5. The TDD gate + hooks refuse to close the task until every scenario
     goes green.

Scope choices:

  * **Gherkin subset only.** We parse Feature / Scenario / Given-When-Then /
    And-But / tags / comments / free-text description. We do NOT yet parse
    Examples tables, Backgrounds, Rules, DocStrings, or Data Tables —
    those land as follow-ups when a concrete test needs them.
  * **No external dep.** Using :mod:`gherkin-official` would give us full
    spec compatibility, but it pulls in a Java-style AST and a heavier
    runtime. Parsing the subset we need is ~80 lines of Python and keeps
    the dep surface flat.

DbC / LOD: the parser is pure (bytes in, value out); ``load_spec_directory``
is total (one broken file never kills the sweep). Callers handle dispatch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "GherkinParseError",
    "Scenario",
    "Specification",
    "Step",
    "load_feature",
    "load_spec_directory",
    "render_pytest_bdd_scaffold",
]


_TAG_LINE_RE = re.compile(r"^\s*(@[\w:.-]+(?:\s+@[\w:.-]+)*)\s*$")
_FEATURE_RE = re.compile(r"^\s*Feature:\s*(.+?)\s*$")
_SCENARIO_RE = re.compile(r"^\s*Scenario(?:\sOutline)?:\s*(.+?)\s*$")
_STEP_RE = re.compile(r"^\s*(Given|When|Then|And|But)\s+(.+?)\s*$")
_COMMENT_RE = re.compile(r"^\s*#")

_STEP_KEYWORDS: frozenset[str] = frozenset({"Given", "When", "Then", "And", "But"})


class GherkinParseError(ValueError):
    """Raised when a ``.feature`` file doesn't parse into a valid Specification."""


@dataclass(slots=True, frozen=True)
class Step:
    """One Given/When/Then/And/But line from a scenario."""

    keyword: str
    text: str


@dataclass(slots=True, frozen=True)
class Scenario:
    """One Gherkin scenario: a named sequence of steps."""

    name: str
    steps: tuple[Step, ...]
    tags: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class Specification:
    """A parsed ``.feature`` file."""

    feature: str
    description: str
    source: Path
    scenarios: tuple[Scenario, ...]
    tags: tuple[str, ...]


# ── Loader ──────────────────────────────────────────────────────────────────


def load_feature(path: Path) -> Specification:
    """Parse a single ``.feature`` file into a :class:`Specification`.

    Raises :class:`GherkinParseError` on any structural problem so callers
    can either skip the file (directory sweep) or surface the error to the
    user (single-file CLI).
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    @dataclass(slots=True)
    class _ScenarioAccumulator:
        """Mutable builder for a scenario-in-progress — internal to ``load_feature``."""

        name: str
        tags: tuple[str, ...]
        steps: list[Step]

    feature_name: str | None = None
    description_lines: list[str] = []
    feature_tags: tuple[str, ...] = ()
    scenarios: list[Scenario] = []

    pending_tags: tuple[str, ...] = ()
    in_feature_description = False
    current_scenario: _ScenarioAccumulator | None = None

    def _close_current_scenario() -> None:
        nonlocal current_scenario
        if current_scenario is None:
            return
        if not current_scenario.steps:
            raise GherkinParseError(f"{path}: scenario {current_scenario.name!r} has no step lines")
        scenarios.append(
            Scenario(
                name=current_scenario.name,
                steps=tuple(current_scenario.steps),
                tags=current_scenario.tags,
            )
        )
        current_scenario = None

    for raw_line in lines:
        stripped = raw_line.strip()

        if not stripped or _COMMENT_RE.match(raw_line):
            continue

        if (m := _TAG_LINE_RE.match(raw_line)) is not None:
            pending_tags = tuple(tag for tag in m.group(1).split() if tag)
            continue

        if (m := _FEATURE_RE.match(raw_line)) is not None:
            if feature_name is not None:
                raise GherkinParseError(f"{path}: multiple Feature blocks not supported")
            feature_name = m.group(1)
            feature_tags = pending_tags
            pending_tags = ()
            in_feature_description = True
            continue

        if (m := _SCENARIO_RE.match(raw_line)) is not None:
            in_feature_description = False
            _close_current_scenario()
            current_scenario = _ScenarioAccumulator(
                name=m.group(1),
                tags=pending_tags,
                steps=[],
            )
            pending_tags = ()
            continue

        if (m := _STEP_RE.match(raw_line)) is not None and current_scenario is not None:
            current_scenario.steps.append(Step(keyword=m.group(1), text=m.group(2)))
            in_feature_description = False
            continue

        if in_feature_description and feature_name is not None:
            description_lines.append(stripped)
            continue

        # Anything else is unparsed; we choose to be lenient rather than strict
        # — the harness should never crash on a free-text blurb inside a spec.

    _close_current_scenario()

    if feature_name is None:
        raise GherkinParseError(f"{path}: no Feature: line found")

    return Specification(
        feature=feature_name,
        description="\n".join(description_lines).strip(),
        source=path,
        scenarios=tuple(scenarios),
        tags=feature_tags,
    )


def load_spec_directory(directory: Path) -> tuple[Specification, ...]:
    """Load every ``*.feature`` file under ``directory`` (non-recursive).

    Returns an empty tuple if the directory doesn't exist. Files that fail
    to parse are silently skipped so one broken spec never kills the sweep.
    """
    if not directory.is_dir():
        return ()
    out: list[Specification] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".feature":
            continue
        try:
            out.append(load_feature(path))
        except GherkinParseError:
            continue
    return tuple(out)


# ── pytest-bdd scaffold ─────────────────────────────────────────────────────


_SCAFFOLD_HEADER = (
    "# Auto-generated by maxwell-daemon spec — replace step bodies with real code.\n"
    "from pytest_bdd import given, scenarios, then, when\n\n"
)


def render_pytest_bdd_scaffold(spec: Specification) -> str:
    """Emit a pytest-bdd test module skeleton for ``spec``.

    Output has:
      * ``scenarios(\"<feature-path>\")`` binding at module top
      * one ``@given/@when/@then`` stub per unique step text
        (``And``/``But`` steps are mapped to their parent keyword based
        on the preceding step, matching pytest-bdd's resolution model)
    """
    feature_rel = str(spec.source).replace("\\", "/")
    body_lines: list[str] = [_SCAFFOLD_HEADER, f'scenarios("{feature_rel}")\n\n']

    emitted: set[tuple[str, str]] = set()
    for scenario in spec.scenarios:
        last_primary = "Given"
        for step in scenario.steps:
            primary = step.keyword if step.keyword in {"Given", "When", "Then"} else last_primary
            key = (primary, step.text)
            if key in emitted:
                last_primary = primary
                continue
            emitted.add(key)
            decorator = primary.lower()
            body_lines.append(
                f'@{decorator}("{step.text}")\n'
                f"def _{primary.lower()}_{_slug(step.text)}():\n"
                "    raise NotImplementedError\n\n"
            )
            last_primary = primary

    return "".join(body_lines)


def _slug(text: str, *, limit: int = 40) -> str:
    """Cheap identifier-safe slug for step function names — purely cosmetic."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return slug[:limit] or "step"
