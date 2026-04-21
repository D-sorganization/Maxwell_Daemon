"""Config loading, env substitution, and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from maxwell_daemon.config import MaxwellDaemonConfig, load_config, save_config
from maxwell_daemon.config.loader import _substitute_env


class TestEnvSubstitution:
    def test_replaces_set_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_KEY", "secret-value")
        assert _substitute_env("${TEST_KEY}") == "secret-value"

    def test_uses_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_SUCH_VAR", raising=False)
        assert _substitute_env("${NO_SUCH_VAR:-fallback}") == "fallback"

    def test_empty_when_unset_no_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_SUCH_VAR", raising=False)
        assert _substitute_env("${NO_SUCH_VAR}") == ""

    def test_recurses_into_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("X", "v")
        assert _substitute_env({"k": "${X}"}) == {"k": "v"}

    def test_recurses_into_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("X", "v")
        assert _substitute_env(["${X}", "plain"]) == ["v", "plain"]

    def test_passes_through_non_strings(self) -> None:
        assert _substitute_env(42) == 42
        assert _substitute_env(None) is None


class TestConfigLoad:
    def test_rejects_empty_backends(self) -> None:
        with pytest.raises(ValueError, match="At least one backend"):
            MaxwellDaemonConfig(backends={})

    def test_load_roundtrip(self, tmp_path: Path) -> None:
        original = MaxwellDaemonConfig.model_validate(
            {
                "backends": {
                    "claude": {"type": "claude", "model": "claude-sonnet-4-6"},
                },
            }
        )
        path = tmp_path / "maxwell-daemon.yaml"
        save_config(original, path)
        loaded = load_config(path)
        assert loaded.backends["claude"].model == "claude-sonnet-4-6"

    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")

    def test_env_substitution_in_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_KEY", "sk-test-123")
        path = tmp_path / "c.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "backends": {
                        "claude": {
                            "type": "claude",
                            "model": "claude-sonnet-4-6",
                            "api_key": "${MY_KEY}",
                        }
                    }
                }
            )
        )
        cfg = load_config(path)
        # api_key is a SecretStr so it doesn't leak in repr / JSON dumps.
        assert cfg.backends["claude"].api_key_value() == "sk-test-123"
        assert "sk-test-123" not in repr(cfg.backends["claude"])

    def test_default_backend_must_exist(self) -> None:
        from pydantic import ValidationError

        # Validation now happens eagerly at model construction (not lazily).
        with pytest.raises(ValidationError, match="not defined in backends"):
            MaxwellDaemonConfig.model_validate(
                {
                    "backends": {"claude": {"type": "claude", "model": "claude-sonnet-4-6"}},
                    "agent": {"default_backend": "nonexistent"},
                }
            )

    def test_default_backend_config_returns_selected_backend(self) -> None:
        cfg = MaxwellDaemonConfig.model_validate(
            {
                "backends": {"claude": {"type": "claude", "model": "claude-sonnet-4-6"}},
                "agent": {"default_backend": "claude"},
            }
        )
        assert cfg.default_backend_config().model == "claude-sonnet-4-6"

    def test_rejects_unknown_top_level_keys(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MaxwellDaemonConfig.model_validate(
                {
                    "backends": {"c": {"type": "claude", "model": "x"}},
                    "bogus_key": True,
                }
            )


class TestDefaultConfigPath:
    def test_maxwell_config_env_overrides_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maxwell_daemon.config.loader import default_config_path

        custom = tmp_path / "custom.yaml"
        monkeypatch.setenv("MAXWELL_CONFIG", str(custom))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert default_config_path() == custom

    def test_xdg_config_home_respected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maxwell_daemon.config.loader import default_config_path

        monkeypatch.delenv("MAXWELL_CONFIG", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        result = default_config_path()
        assert str(tmp_path) in str(result)
        assert "maxwell-daemon" in str(result)
