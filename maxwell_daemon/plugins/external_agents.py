"""Validation-only descriptors for external-agent plugins.

This layer stays intentionally narrow. It validates local metadata, loads
descriptor files, and registers validated descriptors. Runtime adapter
construction stays out of scope.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ExternalAgentPluginDescriptor",
    "ExternalAgentPluginRegistry",
    "PluginDescriptorError",
    "load_external_agent_plugin_descriptor",
    "load_external_agent_plugin_registry",
]


class PluginDescriptorError(RuntimeError):
    """Raised when a plugin descriptor cannot be parsed or registered."""


_ENTRYPOINT_RE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$")


def _require_text(value: Any, *, field_name: str, source: str) -> str:
    if not isinstance(value, str):
        raise PluginDescriptorError(
            f"{source}: field '{field_name}' must be a string, got {type(value).__name__}"
        )
    text = value.strip()
    if not text:
        raise PluginDescriptorError(f"{source}: field '{field_name}' cannot be empty")
    return text


def _coerce_capabilities(value: Any, *, source: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PluginDescriptorError(
            f"{source}: field 'capabilities' must be a list, got {type(value).__name__}"
        )
    capabilities: list[str] = []
    for capability in value:
        if not isinstance(capability, str):
            raise PluginDescriptorError(
                f"{source}: capability names must be strings, got {type(capability).__name__}"
            )
        text = capability.strip()
        if not text:
            raise PluginDescriptorError(f"{source}: capability names cannot be empty")
        capabilities.append(text)
    return tuple(capabilities)


@dataclass(slots=True, frozen=True)
class ExternalAgentPluginDescriptor:
    """Schema for a single external-agent plugin descriptor."""

    name: str
    kind: str
    entrypoint: str
    version: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self._validate("in-memory descriptor")

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, Any], *, source: str = "plugin descriptor"
    ) -> ExternalAgentPluginDescriptor:
        name = _require_text(payload.get("name"), field_name="name", source=source)
        kind = _require_text(payload.get("kind"), field_name="kind", source=source)
        entrypoint = _require_text(
            payload.get("entrypoint"), field_name="entrypoint", source=source
        )
        version = _require_text(payload.get("version"), field_name="version", source=source)
        capabilities = _coerce_capabilities(payload.get("capabilities"), source=source)

        if kind != "external-agent":
            raise PluginDescriptorError(
                f"{source}: field 'kind' must be 'external-agent', got {kind!r}"
            )
        if not _ENTRYPOINT_RE.fullmatch(entrypoint):
            raise PluginDescriptorError(
                f"{source}: field 'entrypoint' must look like 'module:attribute'"
            )

        return cls(
            name=name,
            kind=kind,
            entrypoint=entrypoint,
            version=version,
            capabilities=capabilities,
        )

    def _validate(self, source: str) -> None:
        if self.kind != "external-agent":
            raise PluginDescriptorError(
                f"{source}: field 'kind' must be 'external-agent', got {self.kind!r}"
            )
        if not _ENTRYPOINT_RE.fullmatch(self.entrypoint):
            raise PluginDescriptorError(
                f"{source}: field 'entrypoint' must look like 'module:attribute'"
            )


class ExternalAgentPluginRegistry:
    """Registry of validated external-agent plugin descriptors."""

    def __init__(self) -> None:
        self._descriptors: dict[str, ExternalAgentPluginDescriptor] = {}

    def register(self, descriptor: ExternalAgentPluginDescriptor) -> None:
        if descriptor.name in self._descriptors:
            raise PluginDescriptorError(f"plugin '{descriptor.name}' already registered")
        self._descriptors[descriptor.name] = descriptor

    def get(self, name: str) -> ExternalAgentPluginDescriptor:
        try:
            return self._descriptors[name]
        except KeyError as exc:
            raise PluginDescriptorError(f"unknown plugin '{name}'") from exc

    def available(self) -> list[str]:
        return sorted(self._descriptors)

    def descriptors(self) -> tuple[ExternalAgentPluginDescriptor, ...]:
        return tuple(self._descriptors[name] for name in self.available())

    def load_directory(self, root: Path) -> None:
        for descriptor_path in sorted(root.rglob("plugin.json")):
            self.register(load_external_agent_plugin_descriptor(descriptor_path))


def load_external_agent_plugin_descriptor(path: Path) -> ExternalAgentPluginDescriptor:
    if not path.is_file():
        raise PluginDescriptorError(f"{path}: descriptor file not found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PluginDescriptorError(f"{path}: invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise PluginDescriptorError(f"{path}: descriptor must decode to a JSON object")
    return ExternalAgentPluginDescriptor.from_mapping(payload, source=str(path))


def load_external_agent_plugin_registry(root: Path) -> ExternalAgentPluginRegistry:
    registry = ExternalAgentPluginRegistry()
    registry.load_directory(root)
    return registry
