"""Per-repo overrides resolved into a single settings object.

Separates the *lookup* (name → RepoConfig) from the *application* (RepoConfig →
IssueExecutor args). Callers ask ``resolve_overrides(cfg, repo=name)`` and get
back a ``RepoOverrides`` dataclass with every per-repo knob; anything not
explicitly overridden in config is ``None`` so the caller knows to fall back to
its own default.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from maxwell_daemon.config import MaxwellDaemonConfig

__all__ = ["RepoOverrides", "resolve_overrides"]


@dataclass(slots=True, frozen=True)
class RepoOverrides:
    test_command: list[str] | None = None
    context_max_chars: int | None = None
    max_test_retries: int | None = None
    max_diff_retries: int | None = None
    system_prompt_prefix: str | None = None
    system_prompt_file: Path | None = None


def resolve_overrides(config: MaxwellDaemonConfig, *, repo: str) -> RepoOverrides:
    """Look up per-repo overrides by name. Returns an empty RepoOverrides if
    no matching repo is configured."""
    repo_cfg = next((r for r in config.repos if r.name == repo), None)
    if repo_cfg is None:
        return RepoOverrides()
    return RepoOverrides(
        test_command=repo_cfg.test_command,
        context_max_chars=repo_cfg.context_max_chars,
        max_test_retries=repo_cfg.max_test_retries,
        max_diff_retries=repo_cfg.max_diff_retries,
        system_prompt_prefix=repo_cfg.system_prompt_prefix,
        system_prompt_file=repo_cfg.system_prompt_file,
    )
