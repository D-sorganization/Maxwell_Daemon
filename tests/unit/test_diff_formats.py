"""Tests for the LLM diff-format parsers.

The agent loop asks the model for file edits in one of three formats,
picks whichever the model is most reliable at. The parser tries each
format until one succeeds, so a flaky model answer doesn't wedge the
edit step. See ``maxwell_daemon/editing/diff_formats.py``.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from textwrap import dedent

import pytest

from maxwell_daemon.editing.diff_formats import (
    DiffFormat,
    DiffParseError,
    FileEdit,
    parse_any,
    parse_search_replace,
    parse_udiff,
    parse_whole_file,
)

# ── Shape ────────────────────────────────────────────────────────────────────


class TestFileEdit:
    def test_frozen(self) -> None:
        e = FileEdit(path="x.py", kind="modify", content="", format=DiffFormat.UDIFF)
        with pytest.raises(FrozenInstanceError):
            e.path = "y.py"  # type: ignore[misc]


# ── parse_udiff ──────────────────────────────────────────────────────────────


class TestParseUdiff:
    def test_single_file_single_hunk(self) -> None:
        text = dedent(
            """\
            diff --git a/foo.py b/foo.py
            --- a/foo.py
            +++ b/foo.py
            @@ -1,3 +1,3 @@
             line1
            -old
            +new
             line3
            """
        )
        edits = parse_udiff(text)
        assert len(edits) == 1
        assert edits[0].path == "foo.py"
        assert edits[0].kind == "modify"
        assert edits[0].format is DiffFormat.UDIFF
        assert "-old" in edits[0].content
        assert "+new" in edits[0].content

    def test_two_files_multiple_hunks(self) -> None:
        text = dedent(
            """\
            diff --git a/foo.py b/foo.py
            --- a/foo.py
            +++ b/foo.py
            @@ -1,2 +1,2 @@
            -a
            +A
             b
            @@ -10,2 +10,2 @@
            -x
            +X
             y
            diff --git a/bar.py b/bar.py
            --- a/bar.py
            +++ b/bar.py
            @@ -1,1 +1,1 @@
            -old
            +new
            """
        )
        edits = parse_udiff(text)
        assert [e.path for e in edits] == ["foo.py", "bar.py"]
        # First file should contain both hunk headers (each has opening + closing @@).
        assert edits[0].content.count("@@ -1,2 +1,2 @@") == 1
        assert edits[0].content.count("@@ -10,2 +10,2 @@") == 1

    def test_create_new_file(self) -> None:
        text = dedent(
            """\
            diff --git a/new.py b/new.py
            --- /dev/null
            +++ b/new.py
            @@ -0,0 +1,2 @@
            +hello
            +world
            """
        )
        edits = parse_udiff(text)
        assert len(edits) == 1
        assert edits[0].path == "new.py"
        assert edits[0].kind == "create"

    def test_delete_file(self) -> None:
        text = dedent(
            """\
            diff --git a/gone.py b/gone.py
            --- a/gone.py
            +++ /dev/null
            @@ -1,2 +0,0 @@
            -hello
            -world
            """
        )
        edits = parse_udiff(text)
        assert len(edits) == 1
        assert edits[0].path == "gone.py"
        assert edits[0].kind == "delete"

    def test_malformed_hunk_header_rejected(self) -> None:
        text = dedent(
            """\
            diff --git a/foo.py b/foo.py
            --- a/foo.py
            +++ b/foo.py
            @@ not a real header @@
            -old
            +new
            """
        )
        with pytest.raises(DiffParseError, match="hunk"):
            parse_udiff(text)

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(DiffParseError):
            parse_udiff("")


# ── parse_search_replace ─────────────────────────────────────────────────────


class TestParseSearchReplace:
    def test_single_block(self) -> None:
        text = dedent(
            """\
            file: foo.py
            <<<<<<< SEARCH
            old text
            =======
            new text
            >>>>>>> REPLACE
            """
        )
        edits = parse_search_replace(text)
        assert len(edits) == 1
        e = edits[0]
        assert e.path == "foo.py"
        assert e.kind == "modify"
        assert e.format is DiffFormat.SEARCH_REPLACE
        assert "old text" in e.content
        assert "new text" in e.content

    def test_multiple_blocks_different_files(self) -> None:
        text = dedent(
            """\
            file: foo.py
            <<<<<<< SEARCH
            a
            =======
            A
            >>>>>>> REPLACE

            file: bar.py
            <<<<<<< SEARCH
            b
            =======
            B
            >>>>>>> REPLACE
            """
        )
        edits = parse_search_replace(text)
        assert [e.path for e in edits] == ["foo.py", "bar.py"]

    def test_missing_separator_rejected(self) -> None:
        text = dedent(
            """\
            file: foo.py
            <<<<<<< SEARCH
            old
            new
            >>>>>>> REPLACE
            """
        )
        with pytest.raises(DiffParseError, match="separator"):
            parse_search_replace(text)

    def test_missing_replace_marker_rejected(self) -> None:
        text = dedent(
            """\
            file: foo.py
            <<<<<<< SEARCH
            old
            =======
            new
            """
        )
        with pytest.raises(DiffParseError, match="REPLACE"):
            parse_search_replace(text)

    def test_missing_file_preamble_rejected(self) -> None:
        text = dedent(
            """\
            <<<<<<< SEARCH
            old
            =======
            new
            >>>>>>> REPLACE
            """
        )
        with pytest.raises(DiffParseError, match="file"):
            parse_search_replace(text)


# ── parse_whole_file ─────────────────────────────────────────────────────────


class TestParseWholeFile:
    def test_single_file(self) -> None:
        text = dedent(
            """\
            --- foo.py ---
            print("hello")
            x = 1
            --- end ---
            """
        )
        edits = parse_whole_file(text)
        assert len(edits) == 1
        e = edits[0]
        assert e.path == "foo.py"
        assert e.kind == "modify"
        assert e.format is DiffFormat.WHOLE_FILE
        assert 'print("hello")' in e.content
        assert "x = 1" in e.content

    def test_multiple_files(self) -> None:
        text = dedent(
            """\
            --- foo.py ---
            a = 1
            --- end ---

            --- bar/baz.py ---
            b = 2
            --- end ---
            """
        )
        edits = parse_whole_file(text)
        assert [e.path for e in edits] == ["foo.py", "bar/baz.py"]

    def test_missing_end_rejected(self) -> None:
        text = dedent(
            """\
            --- foo.py ---
            print("hello")
            """
        )
        with pytest.raises(DiffParseError, match="end"):
            parse_whole_file(text)


# ── parse_any ────────────────────────────────────────────────────────────────


class TestParseAny:
    def test_picks_first_working_format(self) -> None:
        text = dedent(
            """\
            diff --git a/foo.py b/foo.py
            --- a/foo.py
            +++ b/foo.py
            @@ -1,1 +1,1 @@
            -old
            +new
            """
        )
        edits = parse_any(text)
        assert len(edits) == 1
        assert edits[0].format is DiffFormat.UDIFF

    def test_falls_through_to_next_format(self) -> None:
        text = dedent(
            """\
            file: foo.py
            <<<<<<< SEARCH
            old
            =======
            new
            >>>>>>> REPLACE
            """
        )
        edits = parse_any(text)
        assert len(edits) == 1
        assert edits[0].format is DiffFormat.SEARCH_REPLACE

    def test_custom_prefer_order_respected(self) -> None:
        # The text is valid whole_file but also happens to be valid udiff?
        # No — whole-file markers don't look like udiff, so udiff would fail.
        # We confirm prefer order is respected by asking for WHOLE_FILE first.
        text = dedent(
            """\
            --- foo.py ---
            a = 1
            --- end ---
            """
        )
        edits = parse_any(text, prefer=(DiffFormat.WHOLE_FILE,))
        assert len(edits) == 1
        assert edits[0].format is DiffFormat.WHOLE_FILE

    def test_all_fail_includes_each_reason(self) -> None:
        with pytest.raises(DiffParseError) as excinfo:
            parse_any("this is not any diff format at all")
        msg = str(excinfo.value)
        assert "udiff" in msg
        assert "search_replace" in msg
        assert "whole_file" in msg
