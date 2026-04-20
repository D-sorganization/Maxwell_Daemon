"""Story templates — shape the LLM's reasoning to the kind of issue at hand.

A DOCS issue wants a careful proofreader; a BUG wants a debugger; a REFACTOR
wants to preserve behaviour. Same JSON output schema, different system-prompt
framings. Classifier runs off labels first, then title keywords, with a
sensible default.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path

__all__ = ["IssueKind", "classify_issue", "render_system_prompt"]


class IssueKind(Enum):
    BUG = "bug"
    FEATURE = "feature"
    DOCS = "docs"
    REFACTOR = "refactor"
    TEST = "test"
    DEFAULT = "default"


_LABEL_MAP: dict[str, IssueKind] = {
    "bug": IssueKind.BUG,
    "regression": IssueKind.BUG,
    "crash": IssueKind.BUG,
    "feature": IssueKind.FEATURE,
    "enhancement": IssueKind.FEATURE,
    "docs": IssueKind.DOCS,
    "documentation": IssueKind.DOCS,
    "typo": IssueKind.DOCS,
    "readme": IssueKind.DOCS,
    "refactor": IssueKind.REFACTOR,
    "cleanup": IssueKind.REFACTOR,
    "test": IssueKind.TEST,
    "tests": IssueKind.TEST,
    "testing": IssueKind.TEST,
}

# Title-keyword classifiers, checked when no label matched.
_TITLE_RULES: tuple[tuple[re.Pattern[str], IssueKind], ...] = (
    (re.compile(r"\b(crash|segfault|exception|traceback|error when)\b", re.I), IssueKind.BUG),
    (re.compile(r"\bbug\b|\bfix\b", re.I), IssueKind.BUG),
    (re.compile(r"\btypo|docs?:|readme\b", re.I), IssueKind.DOCS),
    (re.compile(r"\brefactor|cleanup|simplif", re.I), IssueKind.REFACTOR),
    (re.compile(r"\b(add|support|implement|allow)\b", re.I), IssueKind.FEATURE),
    (re.compile(r"\btest|coverage|pytest\b", re.I), IssueKind.TEST),
)


def classify_issue(*, title: str, body: str, labels: list[str]) -> IssueKind:
    if not isinstance(title, str):
        raise TypeError("title must be str")
    for label in labels:
        kind = _LABEL_MAP.get(label.lower())
        if kind is not None:
            return kind
    for pattern, kind in _TITLE_RULES:
        if pattern.search(title):
            return kind
    return IssueKind.DEFAULT


_TEMPLATE_DIR = Path(__file__).parent / "prompts"


def render_system_prompt(kind: IssueKind) -> str:
    """Load the system prompt for ``kind``. Falls back to DEFAULT on miss."""
    path = _TEMPLATE_DIR / f"{kind.value}.md"
    if not path.is_file():
        path = _TEMPLATE_DIR / "default.md"
    return path.read_text()
