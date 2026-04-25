"""Per-repo config overrides resolved into effective executor settings."""

from __future__ import annotations

import pytest

from maxwell_daemon.config import MaxwellDaemonConfig, RepoConfig
from maxwell_daemon.core.repo_overrides import RepoOverrides, resolve_overrides


def _config(*repo_kwargs: dict) -> MaxwellDaemonConfig:  # type: ignore[type-arg]
    return MaxwellDaemonConfig.model_validate(
        {
            "backends": {"c": {"type": "ollama", "model": "m"}},
            "agent": {"default_backend": "c"},
            "repos": list(repo_kwargs),
        }
    )


class TestRepoConfigModel:
    def test_accepts_override_fields(self) -> None:
        rc = RepoConfig.model_validate(
            {
                "name": "x",
                "path": "/tmp/x",
                "test_command": ["pnpm", "test"],
                "context_max_chars": 32000,
                "max_test_retries": 3,
                "max_diff_retries": 5,
            }
        )
        assert rc.test_command == ["pnpm", "test"]
        assert rc.context_max_chars == 32000
        assert rc.max_test_retries == 3
        assert rc.max_diff_retries == 5

    def test_defaults_are_none(self) -> None:
        rc = RepoConfig.model_validate({"name": "x", "path": "/tmp/x"})
        assert rc.test_command is None
        assert rc.context_max_chars is None

    def test_empty_test_command_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RepoConfig.model_validate(
                {"name": "x", "path": "/tmp/x", "test_command": []}
            )

    def test_negative_retries_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RepoConfig.model_validate(
                {"name": "x", "path": "/tmp/x", "max_test_retries": -1}
            )


class TestResolveOverrides:
    def test_returns_defaults_when_repo_not_configured(self) -> None:
        cfg = _config()
        ov = resolve_overrides(cfg, repo="unknown/repo")
        assert ov == RepoOverrides()

    def test_picks_up_configured_overrides(self) -> None:
        cfg = _config(
            {
                "name": "my-repo",
                "path": "/tmp/x",
                "test_command": ["make", "check"],
                "context_max_chars": 40000,
                "max_test_retries": 4,
            }
        )
        ov = resolve_overrides(cfg, repo="my-repo")
        assert ov.test_command == ["make", "check"]
        assert ov.context_max_chars == 40000
        assert ov.max_test_retries == 4

    def test_matches_by_name_not_path(self) -> None:
        cfg = _config(
            {"name": "configured-name", "path": "/tmp/something"},
        )
        # Caller passes the repo *name* (same as owner/name for GitHub repos).
        assert resolve_overrides(cfg, repo="configured-name").test_command is None
        assert resolve_overrides(cfg, repo="/tmp/something") == RepoOverrides()

    def test_partial_override_leaves_other_fields_none(self) -> None:
        cfg = _config({"name": "x", "path": "/tmp/x", "max_diff_retries": 7})
        ov = resolve_overrides(cfg, repo="x")
        assert ov.max_diff_retries == 7
        assert ov.test_command is None
        assert ov.context_max_chars is None
