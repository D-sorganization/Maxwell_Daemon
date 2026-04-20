"""Unit tests for maxwell_daemon.tools.compression — per-tool output compression.

Tool results (especially ``run_bash`` / ``grep_files``) bloat the agent's context
window. ``ToolResultCompressor`` applies the cheapest strategy that still keeps
the interesting bits: passthrough when short, line de-duplication for
grep-flavoured tools, head/tail truncation when everything else fails.
"""

from __future__ import annotations

import dataclasses

import pytest

from maxwell_daemon.contracts import PreconditionError
from maxwell_daemon.tools.compression import (
    CompressionResult,
    ToolResultCompressor,
)


class TestConstructorPreconditions:
    """DbC: the three size knobs must all be strictly positive."""

    def test_rejects_head_lines_zero(self) -> None:
        with pytest.raises(PreconditionError, match="head_lines"):
            ToolResultCompressor(head_lines=0)

    def test_rejects_head_lines_negative(self) -> None:
        with pytest.raises(PreconditionError, match="head_lines"):
            ToolResultCompressor(head_lines=-5)

    def test_rejects_tail_lines_zero(self) -> None:
        with pytest.raises(PreconditionError, match="tail_lines"):
            ToolResultCompressor(tail_lines=0)

    def test_rejects_tail_lines_negative(self) -> None:
        with pytest.raises(PreconditionError, match="tail_lines"):
            ToolResultCompressor(tail_lines=-1)

    def test_rejects_max_chars_zero(self) -> None:
        with pytest.raises(PreconditionError, match="max_chars"):
            ToolResultCompressor(max_chars=0)

    def test_rejects_max_chars_negative(self) -> None:
        with pytest.raises(PreconditionError, match="max_chars"):
            ToolResultCompressor(max_chars=-100)

    def test_accepts_minimal_positive_values(self) -> None:
        compressor = ToolResultCompressor(head_lines=1, tail_lines=1, max_chars=1)
        # The constructor survived — that's the whole test.
        assert compressor is not None


class TestPassthrough:
    """When output fits under max_chars we hand it back unchanged."""

    def test_short_output_returned_unchanged(self) -> None:
        compressor = ToolResultCompressor(max_chars=100)
        output = "hello world\n"
        result = compressor.compress("run_bash", output)
        assert result.content == output
        assert result.strategy == "passthrough"

    def test_passthrough_at_exact_boundary(self) -> None:
        # len == max_chars is still passthrough (``<=`` per spec).
        output = "x" * 50
        compressor = ToolResultCompressor(max_chars=50)
        result = compressor.compress("run_bash", output)
        assert result.content == output
        assert result.strategy == "passthrough"

    def test_passthrough_unknown_tool_name(self) -> None:
        compressor = ToolResultCompressor(max_chars=100)
        result = compressor.compress("some_tool_we_dont_know", "short")
        assert result.content == "short"
        assert result.strategy == "passthrough"


class TestHeadTailTruncation:
    """Long output keeps the top and bottom with a truncation marker between."""

    def test_head_tail_applied_when_too_long(self) -> None:
        lines = [f"line {i}" for i in range(500)]
        output = "\n".join(lines)
        compressor = ToolResultCompressor(
            head_lines=5, tail_lines=5, max_chars=100
        )
        result = compressor.compress("run_bash", output)
        assert result.strategy == "head_tail"
        # First 5 lines preserved verbatim.
        assert result.content.startswith("line 0\nline 1\nline 2\nline 3\nline 4\n")
        # Last 5 lines preserved verbatim.
        assert result.content.rstrip().endswith(
            "line 495\nline 496\nline 497\nline 498\nline 499"
        )

    def test_head_tail_contains_truncation_marker(self) -> None:
        output = "\n".join(f"L{i}" for i in range(200))
        compressor = ToolResultCompressor(
            head_lines=3, tail_lines=3, max_chars=20
        )
        result = compressor.compress("run_bash", output)
        assert "truncated" in result.content
        # Marker should record how many lines vanished: 200 - 3 - 3 = 194.
        assert "194" in result.content

    def test_head_tail_compressed_shorter_than_original(self) -> None:
        output = "\n".join(f"line {i}" for i in range(1000))
        compressor = ToolResultCompressor(
            head_lines=10, tail_lines=10, max_chars=50
        )
        result = compressor.compress("run_bash", output)
        assert len(result.content) < len(output)

    def test_unknown_tool_still_head_tail_when_long(self) -> None:
        output = "\n".join(f"line {i}" for i in range(400))
        compressor = ToolResultCompressor(
            head_lines=2, tail_lines=2, max_chars=30
        )
        result = compressor.compress("unknown_tool", output)
        assert result.strategy == "head_tail"


class TestDedup:
    """grep_files / glob_files collapse runs of identical consecutive lines."""

    def test_dedup_for_grep_files_collapses_consecutive_dupes(self) -> None:
        # 100 duplicate lines followed by a unique tail — total bytes exceed
        # max_chars so dedup should be chosen over passthrough.
        output = ("match\n" * 100) + "unique_tail\n"
        compressor = ToolResultCompressor(max_chars=50)
        result = compressor.compress("grep_files", output)
        assert result.strategy == "dedup"
        # Only one "match" line survives (consecutive duplicates collapsed).
        assert result.content.count("match\n") == 1
        assert "unique_tail" in result.content

    def test_dedup_for_glob_files_collapses_consecutive_dupes(self) -> None:
        output = ("dup_line\n" * 80) + "last\n"
        compressor = ToolResultCompressor(max_chars=50)
        result = compressor.compress("glob_files", output)
        assert result.strategy == "dedup"
        assert result.content.count("dup_line\n") == 1
        assert "last" in result.content

    def test_dedup_preserves_nonconsecutive_repeats(self) -> None:
        # a/b/a is NOT a consecutive duplicate run — both a's must survive.
        output = ("a\n" * 40) + ("b\n" * 40) + ("a\n" * 40)
        compressor = ToolResultCompressor(max_chars=50)
        result = compressor.compress("grep_files", output)
        assert result.strategy == "dedup"
        assert result.content.count("a\n") == 2
        assert result.content.count("b\n") == 1

    def test_dedup_falls_through_to_head_tail_when_still_too_long(self) -> None:
        # After dedup each line is unique so dedup can't shrink it further —
        # the strategy must then fall through to head_tail.
        lines = [f"unique_{i}" for i in range(500)]
        output = "\n".join(lines)
        compressor = ToolResultCompressor(
            head_lines=3, tail_lines=3, max_chars=50
        )
        result = compressor.compress("grep_files", output)
        assert result.strategy == "head_tail"
        assert "truncated" in result.content

    def test_dedup_not_applied_to_other_tools(self) -> None:
        # run_bash isn't in the dedup set; duplicate lines are untouched when
        # we go through head_tail.
        output = ("same\n" * 200) + "end\n"
        compressor = ToolResultCompressor(
            head_lines=5, tail_lines=5, max_chars=30
        )
        result = compressor.compress("run_bash", output)
        assert result.strategy == "head_tail"

    def test_dedup_short_enough_after_dedup_stays_dedup(self) -> None:
        # After collapsing consecutive dupes the content fits under max_chars,
        # so we stop at "dedup" without escalating to head_tail.
        output = "repeat\n" * 1000  # 7000 chars raw → 7 chars after dedup
        compressor = ToolResultCompressor(max_chars=100)
        result = compressor.compress("grep_files", output)
        assert result.strategy == "dedup"
        assert result.content == "repeat\n"


class TestCompressionResultAccounting:
    """original_bytes / compressed_bytes must match the obvious invariant."""

    def test_original_bytes_matches_input_utf8_length(self) -> None:
        compressor = ToolResultCompressor(max_chars=100)
        output = "hello"
        result = compressor.compress("run_bash", output)
        assert result.original_bytes == len(output.encode("utf-8"))

    def test_compressed_bytes_matches_content_utf8_length(self) -> None:
        compressor = ToolResultCompressor(max_chars=100)
        output = "hello world"
        result = compressor.compress("run_bash", output)
        assert result.compressed_bytes == len(result.content.encode("utf-8"))

    def test_accounting_on_truncation_compressed_is_smaller(self) -> None:
        output = "\n".join(f"line_{i}" for i in range(1000))
        compressor = ToolResultCompressor(
            head_lines=5, tail_lines=5, max_chars=50
        )
        result = compressor.compress("run_bash", output)
        assert result.compressed_bytes < result.original_bytes

    def test_accounting_counts_multibyte_utf8(self) -> None:
        # "€" is 3 bytes in UTF-8 — character count ≠ byte count.
        compressor = ToolResultCompressor(max_chars=100)
        output = "€€€"
        result = compressor.compress("run_bash", output)
        assert result.original_bytes == 9  # 3 chars * 3 bytes
        assert result.compressed_bytes == 9


class TestCompressionResultFrozen:
    """CompressionResult is a frozen dataclass — mutation must fail."""

    def test_frozen_instance_rejects_mutation(self) -> None:
        result = CompressionResult(
            content="hi",
            original_bytes=2,
            compressed_bytes=2,
            strategy="passthrough",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.content = "bye"  # type: ignore[misc]

    def test_frozen_instance_rejects_strategy_mutation(self) -> None:
        result = CompressionResult(
            content="x",
            original_bytes=1,
            compressed_bytes=1,
            strategy="passthrough",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.strategy = "head_tail"  # type: ignore[misc]


class TestEmptyOutput:
    """Empty input is a valid, common case — must not explode."""

    def test_empty_string_returns_empty_content(self) -> None:
        compressor = ToolResultCompressor()
        result = compressor.compress("run_bash", "")
        assert result.content == ""
        assert result.original_bytes == 0
        assert result.compressed_bytes == 0

    def test_empty_string_uses_passthrough(self) -> None:
        compressor = ToolResultCompressor()
        result = compressor.compress("grep_files", "")
        # Empty is under max_chars so passthrough applies.
        assert result.strategy == "passthrough"
