"""Per-tool-name compression strategies for tool output text.

Tool results — especially from ``run_bash`` and ``grep_files`` — routinely
produce thousands of lines that swamp the agent's context window. This module
applies the cheapest strategy that still preserves signal:

1. **passthrough** — the output is already short enough; leave it alone.
2. **dedup** — for grep/glob-style tools, collapse runs of consecutive duplicate
   lines (the common pathology: the same match reported many times).
3. **head_tail** — as a last resort, keep the first N and last M lines with a
   clearly-marked gap in the middle so the model knows something was elided.

The module is a pure standalone utility: it's not wired into ``ToolRegistry``
yet. That integration lives in a follow-up PR.
"""

from __future__ import annotations

from dataclasses import dataclass

from maxwell_daemon.contracts import require

__all__ = [
    "CompressionResult",
    "ToolResultCompressor",
]


@dataclass(slots=True, frozen=True)
class CompressionResult:
    """Outcome of compressing one tool output.

    ``original_bytes`` / ``compressed_bytes`` are UTF-8 byte counts so the
    caller can do accurate token-budget accounting against an LLM context.
    """

    content: str
    original_bytes: int
    compressed_bytes: int
    strategy: str  # "passthrough" | "dedup" | "head_tail"


class ToolResultCompressor:
    """Per-tool-name compression strategies for tool output text.

    The caller creates one instance with the budget knobs and then invokes
    ``compress(tool_name, output)`` for each tool result. The compressor picks
    the right strategy based on the tool name and the output size.
    """

    _TRUNCATION_MARKER = "\n[... {n} lines truncated ...]\n"

    def __init__(
        self,
        *,
        head_lines: int = 50,
        tail_lines: int = 50,
        max_chars: int = 8000,
        dedup_tools: frozenset[str] = frozenset({"grep_files", "glob_files"}),
    ) -> None:
        """Configure compression budgets.

        DbC: ``head_lines``, ``tail_lines``, and ``max_chars`` must all be
        strictly positive. A zero/negative budget makes no sense and always
        reflects a caller bug, so we refuse it loudly rather than silently
        producing empty output.
        """
        require(head_lines >= 1, f"head_lines must be >= 1 (got {head_lines})")
        require(tail_lines >= 1, f"tail_lines must be >= 1 (got {tail_lines})")
        require(max_chars >= 1, f"max_chars must be >= 1 (got {max_chars})")
        self._head_lines = head_lines
        self._tail_lines = tail_lines
        self._max_chars = max_chars
        self._dedup_tools = dedup_tools

    def compress(self, tool_name: str, output: str) -> CompressionResult:
        """Apply the right strategy for this tool. Never raises.

        Decision tree:
          - output fits under ``max_chars`` → passthrough
          - tool in ``dedup_tools`` → dedup; if still too long, head_tail
          - otherwise → head_tail
        """
        original_bytes = len(output.encode("utf-8"))

        # Passthrough — cheapest and most common for small outputs.
        if len(output) <= self._max_chars:
            return CompressionResult(
                content=output,
                original_bytes=original_bytes,
                compressed_bytes=original_bytes,
                strategy="passthrough",
            )

        # Dedup — grep/glob results often repeat the same match line.
        if tool_name in self._dedup_tools:
            deduped = self._dedup_consecutive(output)
            if len(deduped) <= self._max_chars:
                return CompressionResult(
                    content=deduped,
                    original_bytes=original_bytes,
                    compressed_bytes=len(deduped.encode("utf-8")),
                    strategy="dedup",
                )
            # Dedup wasn't enough — fall through to head_tail on the deduped
            # output so we still benefit from the savings.
            output = deduped

        # Head_tail — keep the structural anchors, elide the middle.
        truncated = self._head_tail(output)
        return CompressionResult(
            content=truncated,
            original_bytes=original_bytes,
            compressed_bytes=len(truncated.encode("utf-8")),
            strategy="head_tail",
        )

    @staticmethod
    def _dedup_consecutive(output: str) -> str:
        """Collapse runs of identical consecutive lines into a single line.

        Non-consecutive repeats are preserved — we don't want to hide the
        pattern "a … b … a" by stripping the second ``a``.
        """
        lines = output.splitlines(keepends=True)
        if not lines:
            return output
        deduped: list[str] = [lines[0]]
        for line in lines[1:]:
            if line != deduped[-1]:
                deduped.append(line)
        return "".join(deduped)

    def _head_tail(self, output: str) -> str:
        """Keep the first ``head_lines`` and last ``tail_lines`` with a marker.

        If the output has fewer lines than head+tail we just return it
        verbatim — truncation would be a net loss of information.
        """
        lines = output.splitlines()
        total = len(lines)
        keep = self._head_lines + self._tail_lines
        if total <= keep:
            return output
        head = lines[: self._head_lines]
        tail = lines[-self._tail_lines :]
        elided = total - keep
        marker = self._TRUNCATION_MARKER.format(n=elided)
        return "\n".join(head) + marker + "\n".join(tail)
