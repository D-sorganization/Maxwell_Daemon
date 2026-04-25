"""Config loading, env substitution, and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from maxwell_daemon.config import MaxwellDaemonConfig, load_config, save_config
from maxwell_daemon.config.loader import _substitute_env


class InMemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


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

    def test_load_migrates_plaintext_backend_api_key_into_secret_ref(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "c.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "backends": {
                        "claude": {
                            "type": "claude",
                            "model": "claude-sonnet-4-6",
                            "api_key": "sk-live-123",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        store = InMemorySecretStore()

        cfg = load_config(path, secret_store=store)

        assert cfg.backends["claude"].api_key_value() == "sk-live-123"
        assert (
            cfg.backends["claude"].api_key_secret_ref
            == "maxwell-daemon/backends/claude/api_key"
        )
        assert store.get("maxwell-daemon/backends/claude/api_key") == "sk-live-123"
        rewritten = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert rewritten["backends"]["claude"]["api_key_secret_ref"] == (
            "maxwell-daemon/backends/claude/api_key"
        )
        assert "api_key" not in rewritten["backends"]["claude"]

    def test_load_resolves_backend_api_key_secret_ref(self, tmp_path: Path) -> None:
        path = tmp_path / "c.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "backends": {
                        "claude": {
                            "type": "claude",
                            "model": "claude-sonnet-4-6",
                            "api_key_secret_ref": "maxwell-daemon/backends/claude/api_key",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        store = InMemorySecretStore()
        store.set("maxwell-daemon/backends/claude/api_key", "sk-secret-ref")

        cfg = load_config(path, secret_store=store)

        assert cfg.backends["claude"].api_key_value() == "sk-secret-ref"
        assert (
            cfg.backends["claude"].api_key_secret_ref
            == "maxwell-daemon/backends/claude/api_key"
        )

    def test_save_config_does_not_write_plaintext_when_secret_ref_is_present(
        self, tmp_path: Path
    ) -> None:
        cfg = MaxwellDaemonConfig.model_validate(
            {
                "backends": {
                    "claude": {
                        "type": "claude",
                        "model": "claude-sonnet-4-6",
                        "api_key": "sk-test-123",
                        "api_key_secret_ref": "maxwell-daemon/backends/claude/api_key",
                    }
                }
            }
        )
        path = tmp_path / "saved.yaml"

        save_config(cfg, path)

        saved = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert saved["backends"]["claude"]["api_key_secret_ref"] == (
            "maxwell-daemon/backends/claude/api_key"
        )
        assert "api_key" not in saved["backends"]["claude"]

    def test_default_backend_must_exist(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="not found in backends"):
            MaxwellDaemonConfig.model_validate(
                {
                    "backends": {
                        "claude": {"type": "claude", "model": "claude-sonnet-4-6"}
                    },
                    "agent": {"default_backend": "nonexistent"},
                }
            )

    def test_default_backend_config_returns_selected_backend(self) -> None:
        cfg = MaxwellDaemonConfig.model_validate(
            {
                "backends": {
                    "claude": {"type": "claude", "model": "claude-sonnet-4-6"},
                    "local": {"type": "ollama", "model": "llama3.1"},
                },
                "agent": {"default_backend": "local"},
            }
        )
        assert cfg.default_backend_config().model == "llama3.1"

    def test_rejects_unknown_top_level_keys(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MaxwellDaemonConfig.model_validate(
                {
                    "backends": {"c": {"type": "claude", "model": "x"}},
                    "bogus_key": True,
                }
            )

    def test_memory_config_defaults_to_disabled_dream_cycle(self) -> None:
        cfg = MaxwellDaemonConfig.model_validate(
            {"backends": {"claude": {"type": "claude", "model": "claude-sonnet-4-6"}}}
        )
        assert cfg.memory_dream_interval_seconds == 0
        assert cfg.memory_workspace_path.name == "maxwell-daemon"

    def test_memory_config_expands_workspace_path(self, tmp_path: Path) -> None:
        cfg = MaxwellDaemonConfig.model_validate(
            {
                "backends": {
                    "claude": {"type": "claude", "model": "claude-sonnet-4-6"}
                },
                "memory": {
                    "workspace_path": str(tmp_path),
                    "dream_interval_seconds": 1800,
                },
            }
        )
        assert cfg.memory_workspace_path == tmp_path
        assert cfg.memory_dream_interval_seconds == 1800


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


class TestAPIConfigJWT:
    """Issue #230 — jwt_secret must be wired from config into JWTConfig."""

    def _base_cfg(self) -> dict[str, object]:
        return {
            "backends": {"claude": {"type": "claude", "model": "claude-sonnet-4-6"}}
        }

    def test_jwt_secret_defaults_to_none(self) -> None:
        cfg = MaxwellDaemonConfig.model_validate(self._base_cfg())
        assert cfg.api.jwt_secret is None
        assert cfg.api.jwt_secret_value() is None

    def test_jwt_secret_value_unwraps_secret_str(self) -> None:
        raw = self._base_cfg()
        raw["api"] = {"jwt_secret": "mysecret"}
        cfg = MaxwellDaemonConfig.model_validate(raw)
        assert cfg.api.jwt_secret_value() == "mysecret"
        # Must not leak in repr
        assert "mysecret" not in repr(cfg.api)

    def test_jwt_expiry_seconds_configurable(self) -> None:
        raw = self._base_cfg()
        raw["api"] = {"jwt_secret": "s", "jwt_expiry_seconds": 7200}
        cfg = MaxwellDaemonConfig.model_validate(raw)
        assert cfg.api.jwt_expiry_seconds == 7200

    def test_jwt_config_constructed_when_secret_set(self) -> None:
        """JWTConfig can be built from the config values without error."""
        from maxwell_daemon.auth import JWTConfig

        raw = self._base_cfg()
        raw["api"] = {"jwt_secret": "testsecret123", "jwt_expiry_seconds": 1800}
        cfg = MaxwellDaemonConfig.model_validate(raw)
        secret = cfg.api.jwt_secret_value()
        assert secret is not None
        jwt_cfg = JWTConfig(secret, expiry_seconds=cfg.api.jwt_expiry_seconds)
        assert jwt_cfg.expiry_seconds == 1800
