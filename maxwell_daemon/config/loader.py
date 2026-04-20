"""YAML loader with ${ENV_VAR} substitution."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from maxwell_daemon.config.models import MaxwellDaemonConfig

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


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


def load_config(path: Path | str | None = None) -> MaxwellDaemonConfig:
    p = Path(path).expanduser() if path else default_config_path()
    if not p.exists():
        raise FileNotFoundError(
            f"Config not found at {p}. Run `maxwell-daemon init` to create one."
        )
    with p.open() as f:
        raw = yaml.safe_load(f) or {}
    return MaxwellDaemonConfig.model_validate(_substitute_env(raw))


def save_config(config: MaxwellDaemonConfig, path: Path | str | None = None) -> Path:
    p = Path(path).expanduser() if path else default_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        yaml.safe_dump(
            config.model_dump(mode="json", exclude_none=True),
            f,
            default_flow_style=False,
            sort_keys=False,
        )
    return p
