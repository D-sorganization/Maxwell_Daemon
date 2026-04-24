"""YAML loader with ${ENV_VAR} substitution."""

from __future__ import annotations

import os
import re
from copy import deepcopy
from maxwell_daemon.logging import get_logger
from pathlib import Path
from typing import Any

import yaml

from maxwell_daemon.config.models import MaxwellDaemonConfig
from maxwell_daemon.secrets import KeyringSecretStore, SecretStore, backend_api_key_secret_ref

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")
_LOGGER = get_logger(__name__)


def _substitute_env(value: Any) -> Any:
    """Expand ${VAR} and ${VAR:-default} in strings. Recurses into dicts and lists."""
    if isinstance(value, str):

        def replace(m: re.Match[str]) -> str:
            var, default = m.group(1), m.group(2)
            return os.environ.get(var, default or "")

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


def default_config_path() -> Path:
    override = os.environ.get("MAXWELL_CONFIG")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "maxwell-daemon" / "maxwell-daemon.yaml"


def _write_raw_config(raw: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False)


def _default_secret_store() -> SecretStore | None:
    try:
        return KeyringSecretStore()
    except RuntimeError:
        return None


def _looks_like_env_reference(value: str) -> bool:
    return bool(_ENV_PATTERN.fullmatch(value))


def _migrate_backend_api_keys(
    raw: dict[str, Any], secret_store: SecretStore | None
) -> tuple[dict[str, Any], bool]:
    if secret_store is None:
        return raw, False

    changed = False
    backends = raw.get("backends")
    if not isinstance(backends, dict):
        return raw, False

    for backend_name, backend_cfg in backends.items():
        if not isinstance(backend_cfg, dict):
            continue
        plaintext = backend_cfg.get("api_key")
        if not isinstance(plaintext, str) or not plaintext or _looks_like_env_reference(plaintext):
            continue
        secret_ref = backend_cfg.get("api_key_secret_ref")
        if not isinstance(secret_ref, str) or not secret_ref:
            secret_ref = backend_api_key_secret_ref(str(backend_name))
            backend_cfg["api_key_secret_ref"] = secret_ref
        secret_store.set(secret_ref, plaintext)
        backend_cfg.pop("api_key", None)
        changed = True
        _LOGGER.warning(
            "migrated plaintext backend api_key into keyring-backed secret_ref",
            extra={"backend": backend_name, "secret_ref": secret_ref},
        )
    return raw, changed


def _resolve_backend_api_keys(
    raw: dict[str, Any], secret_store: SecretStore | None
) -> dict[str, Any]:
    backends = raw.get("backends")
    if not isinstance(backends, dict):
        return raw

    for backend_name, backend_cfg in backends.items():
        if not isinstance(backend_cfg, dict):
            continue
        secret_ref = backend_cfg.get("api_key_secret_ref")
        if not isinstance(secret_ref, str) or not secret_ref:
            continue
        if secret_store is None:
            raise RuntimeError(
                f"backend '{backend_name}' uses api_key_secret_ref but no secret store is available"
            )
        secret = secret_store.get(secret_ref)
        if secret is None:
            raise RuntimeError(
                f"backend '{backend_name}' secret_ref '{secret_ref}' was not found in the secret store"
            )
        backend_cfg["api_key"] = secret
    return raw


def load_config(
    path: Path | str | None = None,
    *,
    secret_store: SecretStore | None = None,
) -> MaxwellDaemonConfig:
    p = Path(path).expanduser() if path else default_config_path()
    if not p.exists():
        raise FileNotFoundError(
            f"Config not found at {p}. Run `maxwell-daemon init` to create one."
        )
    with p.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    active_secret_store = secret_store if secret_store is not None else _default_secret_store()
    migrated_raw, changed = _migrate_backend_api_keys(deepcopy(raw), active_secret_store)
    if changed:
        _write_raw_config(migrated_raw, p)
    resolved_raw = _resolve_backend_api_keys(deepcopy(migrated_raw), active_secret_store)
    return MaxwellDaemonConfig.model_validate(_substitute_env(resolved_raw))


def save_config(config: MaxwellDaemonConfig, path: Path | str | None = None) -> Path:
    p = Path(path).expanduser() if path else default_config_path()
    payload = config.model_dump(mode="json", exclude_none=True)
    backends = payload.get("backends")
    if isinstance(backends, dict):
        for backend_name, backend_cfg in config.backends.items():
            backend_payload = backends.get(backend_name)
            if isinstance(backend_payload, dict) and backend_cfg.api_key_secret_ref:
                backend_payload.pop("api_key", None)
    _write_raw_config(payload, p)
    return p
