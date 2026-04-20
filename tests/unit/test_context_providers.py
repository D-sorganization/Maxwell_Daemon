"""Tests for the typed context-provider layer.

A context provider is a named, injectable source of text the agent's
system prompt can pull from. The registry hands them out by name;
``assemble`` composes a budget-respecting prompt block.

All providers are pure (or IO-injected) so tests don't touch the
filesystem or network unless they opt in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.context.providers import (
    ContextProviderRegistry,
    ContextProviderResult,
    InlineTextProvider,
    assemble_context,
)


@pytest.fixture
def registry() -> ContextProviderRegistry:
    return ContextProviderRegistry()


# ── Shape ────────────────────────────────────────────────────────────────────


class TestShapes:
    def test_result_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        r = ContextProviderResult(name="x", text="y", size_chars=1)
        with pytest.raises(FrozenInstanceError):
            r.text = "z"  # type: ignore[misc]


# ── Registry ─────────────────────────────────────────────────────────────────


class TestRegistry:
    def test_register_and_get(self, registry: ContextProviderRegistry) -> None:
        p = InlineTextProvider(name="issue", body="hello")
        registry.register(p)
        assert registry.get("issue") is p

    def test_duplicate_name_rejected(self, registry: ContextProviderRegistry) -> None:
        registry.register(InlineTextProvider(name="issue", body="a"))
        with pytest.raises(KeyError, match="issue"):
            registry.register(InlineTextProvider(name="issue", body="b"))

    def test_unknown_name_raises(self, registry: ContextProviderRegistry) -> None:
        with pytest.raises(KeyError, match="missing"):
            registry.get("missing")

    def test_names_sorted(self, registry: ContextProviderRegistry) -> None:
        registry.register(InlineTextProvider(name="z", body=""))
        registry.register(InlineTextProvider(name="a", body=""))
        assert registry.names() == ["a", "z"]


# ── InlineTextProvider (baseline provider) ──────────────────────────────────


class TestInlineTextProvider:
    async def test_renders_body(self) -> None:
        p = InlineTextProvider(name="issue", body="hello world")
        result = await p.render(query="anything", budget_chars=1000)
        assert result.text == "hello world"
        assert result.size_chars == len("hello world")
        assert result.name == "issue"

    async def test_truncates_to_budget(self) -> None:
        body = "x" * 2000
        p = InlineTextProvider(name="big", body=body)
        result = await p.render(query="", budget_chars=100)
        assert len(result.text) <= 100
        assert "truncated" in result.text.lower()

    async def test_query_unused_by_inline(self) -> None:
        """InlineTextProvider ignores the query arg — it's a pure text source."""
        p = InlineTextProvider(name="x", body="same")
        a = await p.render(query="one", budget_chars=100)
        b = await p.render(query="two", budget_chars=100)
        assert a.text == b.text


# ── assemble_context ────────────────────────────────────────────────────────


class TestAssembleContext:
    async def test_empty_registry_yields_empty(self) -> None:
        reg = ContextProviderRegistry()
        text = await assemble_context(reg, requested=(), query="", total_budget=1000)
        assert text == ""

    async def test_concatenates_providers_in_requested_order(self) -> None:
        reg = ContextProviderRegistry()
        reg.register(InlineTextProvider(name="a", body="AAA"))
        reg.register(InlineTextProvider(name="b", body="BBB"))
        text = await assemble_context(reg, requested=("b", "a"), query="", total_budget=1000)
        # Each block is prefixed with "## <name>" so the model can see which is which.
        assert text.index("BBB") < text.index("AAA")
        assert "## b" in text
        assert "## a" in text

    async def test_budget_split_across_providers(self) -> None:
        reg = ContextProviderRegistry()
        reg.register(InlineTextProvider(name="a", body="x" * 1000))
        reg.register(InlineTextProvider(name="b", body="y" * 1000))
        text = await assemble_context(reg, requested=("a", "b"), query="", total_budget=400)
        # Each provider got roughly half the budget.
        assert len(text) <= 600  # some overhead for headers + truncation markers
        assert "x" in text
        assert "y" in text

    async def test_unknown_provider_name_raises(self) -> None:
        reg = ContextProviderRegistry()
        with pytest.raises(KeyError, match="nope"):
            await assemble_context(reg, requested=("nope",), query="", total_budget=1000)


# ── File-backed provider (DocsProvider) ─────────────────────────────────────


class TestDocsProvider:
    async def test_reads_first_matching_file(self, tmp_path: Path) -> None:
        from maxwell_daemon.context.providers import DocsProvider

        (tmp_path / "CLAUDE.md").write_text("# Project rules\nuse ruff")
        (tmp_path / "CONTRIBUTING.md").write_text("# Contributing\n...")

        p = DocsProvider(workspace=tmp_path, candidates=("CLAUDE.md", "CONTRIBUTING.md"))
        result = await p.render(query="", budget_chars=1000)
        assert "Project rules" in result.text
        assert "Contributing" not in result.text  # only first match used

    async def test_empty_when_no_docs_exist(self, tmp_path: Path) -> None:
        from maxwell_daemon.context.providers import DocsProvider

        p = DocsProvider(workspace=tmp_path, candidates=("CLAUDE.md",))
        result = await p.render(query="", budget_chars=1000)
        assert result.text == ""
        assert result.size_chars == 0


# ── Preconditions ────────────────────────────────────────────────────────────


class TestPreconditions:
    def test_registry_requires_positive_budget_at_assemble(self) -> None:
        pass  # enforced via PreconditionError in assemble — covered below

    async def test_zero_budget_rejected(self) -> None:
        from maxwell_daemon.contracts import PreconditionError

        reg = ContextProviderRegistry()
        reg.register(InlineTextProvider(name="a", body="x"))
        with pytest.raises(PreconditionError, match="budget"):
            await assemble_context(reg, requested=("a",), query="", total_budget=0)
