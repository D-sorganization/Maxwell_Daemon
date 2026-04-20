"""Director — run a vision through an injected decomposer and validate the plan.

The actual LLM call lives inside the caller-supplied ``DecomposerFn`` coroutine
so this module stays provider-agnostic and trivially testable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from maxwell_daemon.contracts import require
from maxwell_daemon.director.types import Decomposition

__all__ = ["DecomposerFn", "Director"]


DecomposerFn = Callable[[str], Awaitable[Decomposition]]


class Director:
    """Orchestrates a single vision → plan run."""

    __slots__ = ("_decomposer",)

    def __init__(self, *, decomposer: DecomposerFn) -> None:
        self._decomposer = decomposer

    async def plan(self, vision: str) -> Decomposition:
        """Run the decomposer, validate the result, and return it.

        :param vision: Non-empty free-text goal. Whitespace-only strings are
            rejected as a precondition.
        :raises PreconditionError: If the vision is empty/whitespace, or if the
            returned :class:`Decomposition` fails structural validation.
        """
        require(bool(vision and vision.strip()), "vision must be non-empty")
        result = await self._decomposer(vision)
        result.validate()
        return result
