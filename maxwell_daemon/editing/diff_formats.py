"""Multi-format diff parser for model-produced file edits.

Aider's insight: no single diff format is reliably produced by every
model. Instead of fighting the model, we let it pick whichever format
it's best at and parse all of them. :func:`parse_any` tries each format
in a caller-supplied order; on total failure it surfaces every format's
rejection reason so the dispatch layer can log *why* none matched.

Three formats are supported:

* ``udiff`` — unified diff (``diff --git`` + ``@@`` hunks).
* ``search_replace`` — Aider-style ``<<<<<<< SEARCH`` / ``=======`` /
  ``>>>>>>> REPLACE`` blocks with a ``file: path`` preamble.
* ``whole_file`` — entire-file blocks between ``--- path ---`` /
  ``--- end ---`` delimiters; safest for small-file regeneration.

The parsers produce a uniform :class:`FileEdit` value so downstream apply
logic doesn't care which format the model used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal

__all__ = [
    "DiffFormat",
    "DiffParseError",
    "FileEdit",
    "parse_any",
    "parse_search_replace",
    "parse_udiff",
    "parse_whole_file",
]


class DiffFormat(str, Enum):
    """Which diff convention produced a :class:`FileEdit`."""

    UDIFF = "udiff"
    SEARCH_REPLACE = "search_replace"
    WHOLE_FILE = "whole_file"


class DiffParseError(ValueError):
    """Raised when text doesn't parse as the requested (or any) diff format."""


EditKind = Literal["modify", "create", "delete"]


@dataclass(slots=True, frozen=True)
class FileEdit:
    """One file change extracted from model output.

    ``content`` meaning depends on ``kind`` and ``format``:

    * ``modify`` + ``UDIFF`` → the hunk text (including ``@@`` headers).
    * ``modify`` + ``SEARCH_REPLACE`` → the raw SEARCH/REPLACE block.
    * ``modify`` + ``WHOLE_FILE`` → the full new file contents.
    * ``create`` → new file contents (for ``WHOLE_FILE``) or hunk (for ``UDIFF``).
    * ``delete`` → empty string.
    """

    path: str
    kind: EditKind
    content: str
    format: DiffFormat


# ── Unified diff ────────────────────────────────────────────────────────────

_GIT_HEADER_RE = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)\s*$")
_MINUS_RE = re.compile(r"^--- (?P<path>.+?)\s*$")
_PLUS_RE = re.compile(r"^\+\+\+ (?P<path>.+?)\s*$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


def parse_udiff(text: str) -> tuple[FileEdit, ...]:
    """Parse unified-diff text into :class:`FileEdit` entries (one per file).

    Accepts ``/dev/null`` on either side to signal create/delete. Raises
    :class:`DiffParseError` for empty input, missing file headers, or any
    hunk header that doesn't match ``@@ -a,b +c,d @@``.
    """
    if not text.strip():
        raise DiffParseError("udiff: empty input")

    lines = text.splitlines()
    # Pre-scan: confirm at least one `diff --git` section exists.
    if not any(_GIT_HEADER_RE.match(line) for line in lines):
        raise DiffParseError("udiff: no 'diff --git' header found")

    edits: list[FileEdit] = []
    i = 0
    n = len(lines)

    while i < n:
        if not _GIT_HEADER_RE.match(lines[i]):
            i += 1
            continue

        # Collect this file's section: from this `diff --git` up to the
        # next one (or EOF).
        section_start = i
        i += 1
        while i < n and not _GIT_HEADER_RE.match(lines[i]):
            i += 1
        section = lines[section_start:i]

        edits.append(_parse_udiff_section(section))

    if not edits:
        raise DiffParseError("udiff: no valid diff sections parsed")
    return tuple(edits)


def _parse_udiff_section(section: list[str]) -> FileEdit:
    if not section:
        raise DiffParseError("udiff: empty section")

    minus_path: str | None = None
    plus_path: str | None = None
    hunk_start_idx: int | None = None

    for idx, line in enumerate(section):
        if (m := _MINUS_RE.match(line)) is not None:
            minus_path = m.group("path").strip()
            continue
        if (m := _PLUS_RE.match(line)) is not None:
            plus_path = m.group("path").strip()
            continue
        if line.startswith("@@"):
            if not _HUNK_RE.match(line):
                raise DiffParseError(f"udiff: malformed hunk header {line!r}")
            hunk_start_idx = idx
            break

    if minus_path is None or plus_path is None:
        raise DiffParseError("udiff: missing '---' or '+++' file header")
    if hunk_start_idx is None:
        raise DiffParseError("udiff: no hunk found in section")

    # Validate every additional `@@` line is a proper hunk header.
    for line in section[hunk_start_idx + 1 :]:
        if line.startswith("@@") and not _HUNK_RE.match(line):
            raise DiffParseError(f"udiff: malformed hunk header {line!r}")

    path, kind = _resolve_udiff_path(minus_path, plus_path)
    content = "\n".join(section[hunk_start_idx:]) + "\n"
    return FileEdit(path=path, kind=kind, content=content, format=DiffFormat.UDIFF)


def _resolve_udiff_path(minus: str, plus: str) -> tuple[str, EditKind]:
    if minus == "/dev/null":
        return _strip_b_prefix(plus), "create"
    if plus == "/dev/null":
        return _strip_a_prefix(minus), "delete"
    return _strip_b_prefix(plus), "modify"


def _strip_a_prefix(path: str) -> str:
    return path[2:] if path.startswith("a/") else path


def _strip_b_prefix(path: str) -> str:
    return path[2:] if path.startswith("b/") else path


# ── SEARCH / REPLACE ────────────────────────────────────────────────────────

_SR_FILE_RE = re.compile(r"^file:\s*(?P<path>\S.*?)\s*$")
_SR_SEARCH_MARKER = "<<<<<<< SEARCH"
_SR_SEPARATOR = "======="
_SR_REPLACE_MARKER = ">>>>>>> REPLACE"


def parse_search_replace(text: str) -> tuple[FileEdit, ...]:
    """Parse Aider-style SEARCH/REPLACE blocks with ``file:`` preambles.

    Each block must be of the form::

        file: path/to/x.py
        <<<<<<< SEARCH
        old lines
        =======
        new lines
        >>>>>>> REPLACE

    Raises :class:`DiffParseError` for missing preamble, missing separator,
    or missing REPLACE marker.
    """
    lines = text.splitlines()
    edits: list[FileEdit] = []
    i = 0
    n = len(lines)
    saw_any_marker = False

    while i < n:
        line = lines[i]
        if line.strip() == _SR_SEARCH_MARKER:
            saw_any_marker = True
            # Walk backwards to find the most recent ``file:`` preamble.
            path = _find_preceding_file_preamble(lines, i)
            if path is None:
                raise DiffParseError(
                    "search_replace: missing 'file: <path>' preamble before SEARCH marker"
                )

            search_lines: list[str] = []
            j = i + 1
            separator_idx: int | None = None
            while j < n:
                if lines[j].strip() == _SR_SEPARATOR:
                    separator_idx = j
                    break
                if lines[j].strip() == _SR_REPLACE_MARKER:
                    # Hit REPLACE before separator => malformed.
                    break
                search_lines.append(lines[j])
                j += 1
            if separator_idx is None:
                raise DiffParseError(
                    "search_replace: missing '=======' separator between SEARCH and REPLACE"
                )

            replace_lines: list[str] = []
            k = separator_idx + 1
            replace_idx: int | None = None
            while k < n:
                if lines[k].strip() == _SR_REPLACE_MARKER:
                    replace_idx = k
                    break
                replace_lines.append(lines[k])
                k += 1
            if replace_idx is None:
                raise DiffParseError("search_replace: missing '>>>>>>> REPLACE' closing marker")

            block = (
                f"{_SR_SEARCH_MARKER}\n"
                + "\n".join(search_lines)
                + f"\n{_SR_SEPARATOR}\n"
                + "\n".join(replace_lines)
                + f"\n{_SR_REPLACE_MARKER}\n"
            )
            edits.append(
                FileEdit(
                    path=path,
                    kind="modify",
                    content=block,
                    format=DiffFormat.SEARCH_REPLACE,
                )
            )
            i = replace_idx + 1
            continue
        i += 1

    if not saw_any_marker:
        raise DiffParseError("search_replace: no '<<<<<<< SEARCH' marker found")
    if not edits:
        raise DiffParseError("search_replace: no complete blocks parsed")
    return tuple(edits)


def _find_preceding_file_preamble(lines: list[str], search_idx: int) -> str | None:
    # Look backwards up to (but not past) any previous SEARCH marker.
    for j in range(search_idx - 1, -1, -1):
        stripped = lines[j].strip()
        if stripped == _SR_REPLACE_MARKER:
            break
        if not stripped:
            continue
        if (m := _SR_FILE_RE.match(lines[j])) is not None:
            return m.group("path")
        # Any other non-blank line without a file preamble invalidates the
        # scan — a preamble must be the nearest non-blank line above SEARCH.
        return None
    return None


# ── Whole file ──────────────────────────────────────────────────────────────

_WF_HEADER_RE = re.compile(r"^---\s+(?P<path>\S.*?)\s+---\s*$")
_WF_END = "--- end ---"


def parse_whole_file(text: str) -> tuple[FileEdit, ...]:
    """Parse whole-file blocks delimited by ``--- path ---`` / ``--- end ---``.

    Raises :class:`DiffParseError` if no header is found, or if any header
    lacks its matching ``--- end ---`` closer.
    """
    lines = text.splitlines()
    edits: list[FileEdit] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        if line.strip() == _WF_END:
            # Stray end marker — treat as malformed.
            raise DiffParseError("whole_file: '--- end ---' without matching header")
        m = _WF_HEADER_RE.match(line)
        if m is None:
            i += 1
            continue
        path = m.group("path")
        body: list[str] = []
        j = i + 1
        end_idx: int | None = None
        while j < n:
            if lines[j].strip() == _WF_END:
                end_idx = j
                break
            if _WF_HEADER_RE.match(lines[j]):
                # New header before close => previous block missing end.
                break
            body.append(lines[j])
            j += 1
        if end_idx is None:
            raise DiffParseError(f"whole_file: missing '--- end ---' closer for {path!r}")
        content = "\n".join(body)
        if body:
            content += "\n"
        edits.append(
            FileEdit(
                path=path,
                kind="modify",
                content=content,
                format=DiffFormat.WHOLE_FILE,
            )
        )
        i = end_idx + 1

    if not edits:
        raise DiffParseError("whole_file: no '--- path ---' headers found")
    return tuple(edits)


# ── Multi-format fallback ───────────────────────────────────────────────────


_PARSERS: dict[DiffFormat, object] = {
    DiffFormat.UDIFF: parse_udiff,
    DiffFormat.SEARCH_REPLACE: parse_search_replace,
    DiffFormat.WHOLE_FILE: parse_whole_file,
}


def parse_any(
    text: str,
    *,
    prefer: tuple[DiffFormat, ...] = (
        DiffFormat.UDIFF,
        DiffFormat.SEARCH_REPLACE,
        DiffFormat.WHOLE_FILE,
    ),
) -> tuple[FileEdit, ...]:
    """Try each format in ``prefer`` order; return the first non-empty result.

    On total failure, raises :class:`DiffParseError` whose message lists
    every attempted format and its rejection reason — critical telemetry
    when a model's output format drifts.
    """
    failures: list[str] = []
    for fmt in prefer:
        parser = _PARSERS[fmt]
        try:
            edits = parser(text)  # type: ignore[operator]
        except DiffParseError as e:
            failures.append(f"{fmt.value}: {e}")
            continue
        if edits:
            return edits  # type: ignore[no-any-return]
        failures.append(f"{fmt.value}: returned no edits")
    raise DiffParseError("parse_any: no format matched — " + "; ".join(failures))
