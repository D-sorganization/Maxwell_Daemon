"""Central pricing table for all LLM providers.

Single source of truth for cost estimation across backends.  Structured
as a nested dict  ``{provider: {model: (input_usd_per_1m, output_usd_per_1m)}}``
so adding a new provider is one dict entry with no other changes needed.

Fall-back behaviour
-------------------
``cost_for(provider, model, usage)`` returns 0.0 and logs a WARNING for
any combination that isn't in the table, rather than crashing or silently
returning a non-zero guess.  This is the safest default: the audit trail
shows $0 for unrecognised models rather than an inflated number that could
trigger false budget alerts.

Adding new providers
--------------------
1. Add an entry to ``_PROVIDER_PRICING`` below.
2. For free/local providers use ``(0.0, 0.0)`` as the rate tuple; the
   helper ``is_free_provider()`` returns ``True`` for them so callers can
   skip cost recording entirely if they prefer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from maxwell_daemon.logging import get_logger

if TYPE_CHECKING:
    from maxwell_daemon.backends.base import TokenUsage

log = get_logger(__name__)

# USD per 1,000,000 tokens (input, output).
# Last updated: 2026-04 — published public list prices for each provider.
# Prices drift; re-check provider pricing pages periodically and bump here.
# If a model you route to is missing, add it rather than letting ``get_rates``
# fall through to the ``(0.0, 0.0)`` warning path — silent $0 charging in the
# ledger defeats the point of cost tracking.
_PROVIDER_PRICING: dict[str, dict[str, tuple[float, float]]] = {
    # ── Anthropic ──────────────────────────────────────────────────────────
    "claude": {
        "claude-opus-4-7": (15.0, 75.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5": (0.80, 4.0),
        "claude-3-5-sonnet-latest": (3.0, 15.0),
        "claude-3-5-haiku-latest": (0.80, 4.0),
    },
    # agent-loop uses the same Anthropic models — alias to the same table.
    "agent-loop": {
        "claude-opus-4-7": (15.0, 75.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5": (0.80, 4.0),
        "claude-3-5-sonnet-latest": (3.0, 15.0),
        "claude-3-5-haiku-latest": (0.80, 4.0),
    },
    # ── OpenAI ────────────────────────────────────────────────────────────
    "openai": {
        "gpt-4o": (2.5, 10.0),
        "gpt-4o-mini": (0.15, 0.60),
        "gpt-4-turbo": (10.0, 30.0),
        "gpt-3.5-turbo": (0.50, 1.50),
        "o1": (15.0, 60.0),
        "o1-mini": (3.0, 12.0),
        "o3-mini": (1.10, 4.40),
    },
    # ── Azure OpenAI ──────────────────────────────────────────────────────
    # Azure uses the same model deployments as OpenAI; pricing is identical
    # for standard deployments (PTUs differ, but we can't know that here).
    "azure": {
        "gpt-4o": (2.5, 10.0),
        "gpt-4o-mini": (0.15, 0.60),
        "gpt-4-turbo": (10.0, 30.0),
        "gpt-3.5-turbo": (0.50, 1.50),
        "o1": (15.0, 60.0),
        "o1-mini": (3.0, 12.0),
        "o3-mini": (1.10, 4.40),
    },
    # ── Ollama (local) — always free ──────────────────────────────────────
    # Ollama runs entirely on local hardware; there is no per-token charge.
    # Any model name is valid and costs nothing.
    "ollama": {},
    "ollama-agent-loop": {},
    # ── Google Gemini ─────────────────────────────────────────────────────
    "gemini": {
        "gemini-1.5-pro": (1.25, 5.0),
        "gemini-1.5-flash": (0.075, 0.30),
        "gemini-2.0-flash": (0.10, 0.40),
        "gemini-2.5-pro": (1.25, 10.0),
        "gemini-2.5-flash": (0.15, 0.60),
        "gemini-2.5-flash-lite": (0.075, 0.30),
    },
    # ── Groq ──────────────────────────────────────────────────────────────
    "groq": {
        "llama-3.3-70b-versatile": (0.59, 0.79),
        "llama-3.1-8b-instant": (0.05, 0.08),
        "mixtral-8x7b-32768": (0.24, 0.24),
        "gemma2-9b-it": (0.20, 0.20),
    },
    # ── Mistral AI ────────────────────────────────────────────────────────
    "mistral": {
        "mistral-large-latest": (2.0, 6.0),
        "mistral-large": (2.0, 6.0),
        "mistral-small-latest": (0.20, 0.60),
        "mistral-small": (0.20, 0.60),
        "codestral-latest": (0.20, 0.60),
        "open-mistral-nemo": (0.15, 0.15),
        "open-mixtral-8x22b": (2.0, 6.0),
    },
    # ── DeepSeek ──────────────────────────────────────────────────────────
    "deepseek": {
        "deepseek-chat": (0.27, 1.10),
        "deepseek-reasoner": (0.55, 2.19),
    },
    # ── Together AI ───────────────────────────────────────────────────────
    "together": {
        "meta-llama/Llama-3.3-70B-Instruct-Turbo": (0.88, 0.88),
        "meta-llama/Llama-3.1-8B-Instruct-Turbo": (0.18, 0.18),
        "mistralai/Mixtral-8x7B-Instruct-v0.1": (0.60, 0.60),
        "Qwen/Qwen2.5-72B-Instruct-Turbo": (1.20, 1.20),
    },
}

#: Providers whose per-token cost is always zero regardless of model.
_FREE_PROVIDERS: frozenset[str] = frozenset({"ollama", "ollama-agent-loop"})


def is_free_provider(provider: str) -> bool:
    """Return True for providers that never incur a token cost."""
    return provider in _FREE_PROVIDERS


def get_rates(provider: str, model: str) -> tuple[float, float]:
    """Return ``(input_usd_per_1m, output_usd_per_1m)`` for *provider* / *model*.

    Falls back to ``(0.0, 0.0)`` with a logged WARNING for unknown
    combinations so callers never crash on an unrecognised model.
    """
    if provider in _FREE_PROVIDERS:
        return (0.0, 0.0)

    provider_table = _PROVIDER_PRICING.get(provider)
    if provider_table is None:
        log.warning(
            "Unknown provider %r — cost tracking disabled for this request. "
            "Add it to maxwell_daemon.backends.pricing._PROVIDER_PRICING.",
            provider,
        )
        return (0.0, 0.0)

    rates = provider_table.get(model)
    if rates is None:
        log.warning(
            "Unknown model %r for provider %r — cost tracking disabled for this request. "
            "Add it to maxwell_daemon.backends.pricing._PROVIDER_PRICING.",
            model,
            provider,
        )
        return (0.0, 0.0)

    return rates


def cost_for(provider: str, model: str, usage: TokenUsage) -> float:
    """Compute total USD cost for a single request.

    Parameters
    ----------
    provider:
        Backend name as registered in the registry (e.g. ``"openai"``,
        ``"claude"``, ``"azure"``).
    model:
        The model string returned by the API (e.g. ``"gpt-4o"``).
    usage:
        Token counts from the API response.
    """
    price_in, price_out = get_rates(provider, model)

    # Provider-specific cache read discounts
    # Anthropic charges ~10% for cache reads (90% discount)
    # OpenAI charges ~50% for cache reads (50% discount)
    cache_discount = 1.0
    if usage.cached_tokens > 0:
        if provider in ("claude", "agent-loop"):
            cache_discount = 0.10
        elif provider in ("openai", "azure"):
            cache_discount = 0.50

    # usage.prompt_tokens usually includes the cached tokens in OpenAI/Anthropic APIs,
    # or the caller adds them. Assuming prompt_tokens is the total input tokens:
    billed_input_tokens = usage.prompt_tokens - usage.cached_tokens
    billed_cached_tokens = usage.cached_tokens

    # Fallback if prompt_tokens didn't include cached_tokens:
    if billed_input_tokens < 0:
        billed_input_tokens = usage.prompt_tokens
        billed_cached_tokens = 0

    input_cost = (billed_input_tokens * price_in / 1_000_000) + (
        billed_cached_tokens * price_in * cache_discount / 1_000_000
    )
    output_cost = usage.completion_tokens * price_out / 1_000_000

    return input_cost + output_cost
