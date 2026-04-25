"""Local plugin descriptors for external agent adapters."""

from __future__ import annotations

import importlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):  # pragma: no cover - exercised by default local runtime
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 in CI
    import tomli as tomllib

from maxwell_daemon.backends.external_adapter import (
    ExternalAgentAdapterError,
    ExternalAgentAdapterProtocol,
    ExternalAgentAdapterRegistry,
)

__all__ = [
    "ExternalAgentPluginDescriptor",
    "load_external_agent_adapter",
    "load_external_agent_plugin_descriptor",
    "load_external_agent_plugin_descriptors",
    "register_external_agent_plugins",
]

_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ENTRYPOINT_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:"
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_SUPPORTED_SUFFIXES = frozenset({".json", ".toml"})


@dataclass(slots=True, frozen=True)
class ExternalAgentPluginDescriptor:
    """Validated local plugin metadata for one external-agent adapter."""

    name: str
    kind: str
    entrypoint: str
    version: str = "1"
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_descriptor(
            _PLUGIN_NAME_RE.match(self.name) is not None, "plugin name must be stable"
        )
        _require_descriptor(
            self.kind == "external-agent", "plugin kind must be 'external-agent'"
        )
        _require_descriptor(
            _ENTRYPOINT_RE.match(self.entrypoint) is not None,
            "plugin entrypoint must be 'module:attribute'",
        )
        _require_descriptor(
            bool(self.version.strip()), "plugin version must be non-empty"
        )
        _require_descriptor(
            len(set(self.capabilities)) == len(self.capabilities),
            "plugin capabilities must not contain duplicates",
        )
        for capability in self.capabilities:
            _require_descriptor(
                bool(capability.strip()),
                "plugin capabilities must be non-empty strings",
            )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ExternalAgentPluginDescriptor:
        capabilities = data.get("capabilities", ())
        if not isinstance(capabilities, (list, tuple)):
            raise ExternalAgentAdapterError("plugin capabilities must be a list")
        try:
            return cls(
                name=str(data["name"]),
                kind=str(data["kind"]),
                entrypoint=str(data["entrypoint"]),
                version=str(data.get("version", "1")),
                capabilities=tuple(str(item) for item in capabilities),
            )
        except KeyError as exc:
            raise ExternalAgentAdapterError(
                f"plugin descriptor missing {exc.args[0]!r}"
            ) from exc


def load_external_agent_plugin_descriptor(
    path: Path | str,
) -> ExternalAgentPluginDescriptor:
    """Load one JSON or TOML external-agent plugin descriptor."""

    descriptor_path = Path(path)
    suffix = descriptor_path.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise ExternalAgentAdapterError(
            f"unsupported plugin descriptor suffix {descriptor_path.suffix!r}"
        )
    data = _parse_descriptor(descriptor_path)
    if not isinstance(data, Mapping):
        raise ExternalAgentAdapterError("plugin descriptor must be an object")
    return ExternalAgentPluginDescriptor.from_mapping(data)


def load_external_agent_plugin_descriptors(
    path: Path | str,
) -> tuple[ExternalAgentPluginDescriptor, ...]:
    """Load descriptors from one file or a directory, sorted deterministically."""

    root = Path(path)
    if root.is_file():
        return (load_external_agent_plugin_descriptor(root),)
    if not root.is_dir():
        raise ExternalAgentAdapterError(
            f"plugin descriptor path does not exist: {root}"
        )
    descriptors = tuple(
        load_external_agent_plugin_descriptor(candidate)
        for candidate in sorted(root.iterdir(), key=lambda item: item.name)
        if candidate.is_file() and candidate.suffix.lower() in _SUPPORTED_SUFFIXES
    )
    _reject_duplicate_plugin_names(descriptors)
    return descriptors


def load_external_agent_adapter(
    descriptor: ExternalAgentPluginDescriptor,
) -> ExternalAgentAdapterProtocol:
    """Import and instantiate the adapter declared by a descriptor."""

    module_name, attr_path = descriptor.entrypoint.split(":", 1)
    try:
        obj: Any = importlib.import_module(module_name)
        for part in attr_path.split("."):
            obj = getattr(obj, part)
    except (ImportError, AttributeError) as exc:
        raise ExternalAgentAdapterError(
            f"failed to load plugin entrypoint {descriptor.entrypoint!r}: {exc}"
        ) from exc

    adapter = obj() if callable(obj) else obj
    if not isinstance(adapter, ExternalAgentAdapterProtocol):
        raise ExternalAgentAdapterError(
            f"plugin {descriptor.name!r} entrypoint did not produce an external agent adapter"
        )
    if adapter.adapter_id != descriptor.name:
        raise ExternalAgentAdapterError(
            f"plugin {descriptor.name!r} adapter id mismatch: {adapter.adapter_id!r}"
        )
    return adapter


def register_external_agent_plugins(
    registry: ExternalAgentAdapterRegistry,
    descriptors: Sequence[ExternalAgentPluginDescriptor],
) -> tuple[str, ...]:
    """Load descriptor entrypoints into a registry and return registered names."""

    _reject_duplicate_plugin_names(descriptors)
    registered: list[str] = []
    for descriptor in descriptors:
        registry.register(load_external_agent_adapter(descriptor))
        registered.append(descriptor.name)
    return tuple(registered)


def _parse_descriptor(path: Path) -> object:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return tomllib.loads(text)


def _require_descriptor(condition: bool, message: str) -> None:
    if not condition:
        raise ExternalAgentAdapterError(message)


def _reject_duplicate_plugin_names(
    descriptors: Sequence[ExternalAgentPluginDescriptor],
) -> None:
    seen: set[str] = set()
    for descriptor in descriptors:
        if descriptor.name in seen:
            raise ExternalAgentAdapterError(
                f"duplicate plugin descriptor {descriptor.name!r}"
            )
        seen.add(descriptor.name)
