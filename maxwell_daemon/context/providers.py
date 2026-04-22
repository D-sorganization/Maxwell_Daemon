"""Typed context providers — named text sources for the agent prompt.

A :class:`ContextProvider` is a named source of text the system prompt can
request on demand. Where tools *do* things, providers *supply* things:
the issue body, the diff, the repo map, the CI profile, past episodes.

Design notes:

* **LOD:** providers don't know the prompt format. They render text;
  :func:`assemble_context` is the only function that knows about section
  headers and budget arithmetic. A new provider is one class; no
  downstream coupling.
* **DRY:** the registry enforces unique names and a stable iteration
  order; callers just ask for ``("issue", "diff", "repo")`` and get
  back a composed block. Switching out one source for another is a
  registration swap, not a refactor.
* **DbC:** :func:`assemble_context` rejects ``total_budget <= 0`` via
  :func:`~maxwell_daemon.contracts.require`; the registry rejects
  duplicate names with :class:`KeyError` at registration time.
* **Reversibility:** every provider has a trivial ``render`` contract
  — bad behaviour is contained to one class.

Scope choice: the two baseline providers shipped here
(:class:`InlineTextProvider`, :class:`DocsProvider`) are the ones the
agent loop needs to bootstrap. Heavier providers — a git-diff provider
that calls out to git, an episodic provider that talks to the SQLite
memory store — live alongside their collaborators so the context module
stays free of sibling-module imports.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from maxwell_daemon.contracts import require

__all__ = [
    "ContextProvider",
    "ContextProviderRegistry",
    "ContextProviderResult",
    "DocsProvider",
    "InlineTextProvider",
    "assemble_context",
]


_TRUNCATION_MARKER = "\n... [truncated] ...\n"


@dataclass(slots=True, frozen=True)
class ContextProviderResult:
    """What one provider returns for one ``render`` call."""

    name: str
    text: str
    size_chars: int


class ContextProvider(Protocol):
    """Structural contract every provider satisfies."""

    name: str

    async def render(self, *, query: str, budget_chars: int) -> ContextProviderResult: ...


# ── Built-in providers ──────────────────────────────────────────────────────


@dataclass(slots=True)
class InlineTextProvider:
    """A provider whose body is known at construction time."""

    name: str
    body: str

    async def render(self, *, query: str, budget_chars: int) -> ContextProviderResult:
        text = _truncate_to_budget(self.body, budget_chars)
        return ContextProviderResult(name=self.name, text=text, size_chars=len(text))


@dataclass(slots=True)
class DocsProvider:
    """First-match-wins file loader for repo-level contributor docs."""

    workspace: Path
    candidates: Sequence[str] = (
        "CLAUDE.md",
        "CONTRIBUTING.md",
        ".github/CONTRIBUTING.md",
    )
    name: str = "docs"

    async def render(self, *, query: str, budget_chars: int) -> ContextProviderResult:
        for candidate in self.candidates:
            path = self.workspace / candidate
            if not path.is_file():
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            text = _truncate_to_budget(body, budget_chars)
            return ContextProviderResult(name=self.name, text=text, size_chars=len(text))

        return ContextProviderResult(name=self.name, text="", size_chars=0)


@dataclass(slots=True)
class RepoSchematicProvider:
    """Provides the structural map of the repository (files and symbols)."""

    workspace: Path
    name: str = "repo_schematic"

    async def render(self, *, query: str, budget_chars: int) -> ContextProviderResult:
        from maxwell_daemon.gh.repo_schematic import build_repo_schematic

        try:
            map_block = build_repo_schematic(self.workspace).to_prompt(max_chars=budget_chars)
            return ContextProviderResult(name=self.name, text=map_block, size_chars=len(map_block))
        except Exception:
            return ContextProviderResult(name=self.name, text="", size_chars=0)


# ── Registry ────────────────────────────────────────────────────────────────


class ContextProviderRegistry:
    """Names to providers. Rejects duplicates; raises on missing names."""

    def __init__(self) -> None:
        self._providers: dict[str, ContextProvider] = {}

    def register(self, provider: ContextProvider) -> None:
        if provider.name in self._providers:
            raise KeyError(f"context provider {provider.name!r} already registered")
        self._providers[provider.name] = provider

    def get(self, name: str) -> ContextProvider:
        if name not in self._providers:
            raise KeyError(f"unknown context provider {name!r}")
        return self._providers[name]

    def names(self) -> list[str]:
        return sorted(self._providers.keys())


# ── Assembly ────────────────────────────────────────────────────────────────


async def assemble_context(
    registry: ContextProviderRegistry,
    *,
    requested: Sequence[str],
    query: str,
    total_budget: int,
) -> str:
    """Render every ``requested`` provider and join into one markdown block."""
    require(
        total_budget > 0,
        f"assemble_context: total_budget must be > 0 (got {total_budget})",
    )
    if not requested:
        return ""

    per_provider_budget = max(1, total_budget // len(requested))
    sections: list[str] = []
    for name in requested:
        provider = registry.get(name)  # KeyError propagates — misconfig surfaces loudly
        result = await provider.render(query=query, budget_chars=per_provider_budget)
        if not result.text:
            continue
        sections.append(f"## {result.name}\n\n{result.text}")
    return "\n\n".join(sections)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _truncate_to_budget(text: str, budget: int) -> str:
    """Keep under ``budget`` chars; append a marker when we cut."""
    if len(text) <= budget:
        return text
    cut = max(0, budget - len(_TRUNCATION_MARKER))
    return text[:cut] + _TRUNCATION_MARKER
