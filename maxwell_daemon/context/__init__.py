"""Typed context providers — named, injectable text sources for the agent prompt.

Context providers are the *pull* side of prompt composition. Where tools
*do* things (run_bash, write_file), providers *supply* things (the issue
body, the diff, the CI profile, the repo map). Decoupling the two gives
us a small set of named sources the system-prompt assembler can request
by name with a token budget.
"""

from maxwell_daemon.context.providers import (
    ContextProvider,
    ContextProviderRegistry,
    ContextProviderResult,
    DocsProvider,
    InlineTextProvider,
    assemble_context,
)

__all__ = [
    "ContextProvider",
    "ContextProviderRegistry",
    "ContextProviderResult",
    "DocsProvider",
    "InlineTextProvider",
    "assemble_context",
]
