"""Conversation-history condensation for long-running agent loops.

A 150-turn agent loop balloons the message list past the model's context
window. Rather than hit a hard error, we summarize a middle slice into a
single synthetic "user" message. Preserved:

  * the first user message (the original task) — the anchor
  * the last ``keep_recent`` messages — fresh context for the next turn

Everything between is replaced by one bracketed summary so the agent still
has *some* signal about what happened earlier.

DbC:
  * ``threshold_tokens`` and ``keep_recent`` must be positive.
  * ``condense()`` is idempotent on short lists — returns the input
    unchanged when there's nothing meaningful to summarize.

LOD:
  * The summarizer is an injected callable; we don't know it's an LLM.
  * ``Condenser`` doesn't touch the Anthropic client or the cost ledger —
    that's the agent loop's job when it invokes condense().
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from maxwell_daemon.contracts import require

__all__ = ["Condenser", "SummarizerFn"]


#: Summarizer signature — takes the middle-slice of messages and returns a
#: one-paragraph summary string. Wired to the LLM at the call site.
SummarizerFn = Callable[[list[dict[str, object]]], Awaitable[str]]


class Condenser:
    """Shrinks a long agent message list by summarizing middle turns.

    Invariants across ``condense()``:
      1. The first message (the task statement) is always retained.
      2. The last ``keep_recent`` messages are always retained verbatim.
      3. If the compressed result wouldn't be shorter than the input, we
         return the input — no point paying for a summary that adds bytes.
    """

    def __init__(
        self,
        *,
        threshold_tokens: int,
        keep_recent: int,
        summarizer: SummarizerFn,
    ) -> None:
        require(
            threshold_tokens >= 1,
            f"threshold_tokens must be >= 1 (got {threshold_tokens})",
        )
        require(keep_recent >= 1, f"keep_recent must be >= 1 (got {keep_recent})")
        self._threshold = threshold_tokens
        self._keep_recent = keep_recent
        self._summarize = summarizer

    def should_condense(self, total_tokens: int) -> bool:
        """True when accumulated prompt tokens have reached the threshold."""
        return total_tokens >= self._threshold

    async def condense(
        self, messages: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Return a shorter message list, or the input if compression isn't useful.

        The output shape is ``[anchor, summary, *tail]`` with the summary as
        a synthetic ``role: user`` message so it doesn't break the
        user/assistant alternation downstream models expect.
        """
        # Anchor (1) + summary (1) + tail (keep_recent) = keep_recent + 2
        # messages after compression. Only worth it if we can drop at least
        # one real middle message.
        minimum_length = self._keep_recent + 2
        if len(messages) <= minimum_length:
            return messages

        anchor = messages[:1]
        tail = messages[-self._keep_recent :]
        middle = messages[1 : -self._keep_recent]
        if not middle:
            return messages

        try:
            summary_text = await self._summarize(list(middle))
        except Exception:
            return messages

        summary_msg: dict[str, object] = {
            "role": "user",
            "content": f"[Prior turns summarized: {summary_text}]",
        }
        return [*anchor, summary_msg, *tail]
