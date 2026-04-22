"""Tests for the external-agent plugin descriptor loader and registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from maxwell_daemon.plugins.external_agents import (
    ExternalAgentPluginDescriptor,
    ExternalAgentPluginRegistry,
    PluginDescriptorError,
    load_external_agent_plugin_descriptor,
    load_external_agent_plugin_registry,
)


@pytest.fixture()
def descriptor_payload() -> dict[str, object]:
    return {
        "name": "aider-cli",
        "kind": "external-agent",
        "entrypoint": "maxwell_daemon.external_agents.aider:AiderAdapter",
        "version": "1",
        "capabilities": ["diff", "git", "stdout-artifacts"],
    }


def _write_descriptor(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestExternalAgentPluginDescriptor:
    def test_valid_descriptor_round_trips(self, descriptor_payload: dict[str, object]) -> None:
        descriptor = ExternalAgentPluginDescriptor.from_mapping(descriptor_payload)

        assert descriptor.name == "aider-cli"
        assert descriptor.kind == "external-agent"
        assert descriptor.entrypoint == "maxwell_daemon.external_agents.aider:AiderAdapter"
        assert descriptor.version == "1"
        assert descriptor.capabilities == ("diff", "git", "stdout-artifacts")

    def test_invalid_kind_is_rejected(self, descriptor_payload: dict[str, object]) -> None:
        descriptor_payload["kind"] = "agent"

        with pytest.raises(PluginDescriptorError, match="must be 'external-agent'"):
            ExternalAgentPluginDescriptor.from_mapping(descriptor_payload)

    def test_invalid_entrypoint_is_rejected(self, descriptor_payload: dict[str, object]) -> None:
        descriptor_payload["entrypoint"] = "maxwell_daemon.external_agents.aider"

        with pytest.raises(PluginDescriptorError, match="must look like 'module:attribute'"):
            ExternalAgentPluginDescriptor.from_mapping(descriptor_payload)


class TestExternalAgentPluginRegistry:
    def test_registry_rejects_duplicate_names(self, descriptor_payload: dict[str, object]) -> None:
        registry = ExternalAgentPluginRegistry()
        descriptor = ExternalAgentPluginDescriptor.from_mapping(descriptor_payload)
        registry.register(descriptor)

        with pytest.raises(PluginDescriptorError, match="already registered"):
            registry.register(descriptor)

    def test_registry_loads_descriptor_files(
        self, tmp_path: Path, descriptor_payload: dict[str, object]
    ) -> None:
        plugin_dir = tmp_path / "aider"
        plugin_dir.mkdir()
        descriptor_path = _write_descriptor(plugin_dir / "plugin.json", descriptor_payload)

        loaded = load_external_agent_plugin_descriptor(descriptor_path)
        registry = load_external_agent_plugin_registry(tmp_path)

        assert loaded.capabilities == ("diff", "git", "stdout-artifacts")
        assert registry.available() == ["aider-cli"]
        assert registry.get("aider-cli") == loaded

    def test_directory_loader_rejects_duplicate_plugin_names(
        self, tmp_path: Path, descriptor_payload: dict[str, object]
    ) -> None:
        first = tmp_path / "aider"
        second = tmp_path / "continue"
        first.mkdir()
        second.mkdir()
        _write_descriptor(first / "plugin.json", descriptor_payload)
        _write_descriptor(second / "plugin.json", descriptor_payload)

        with pytest.raises(PluginDescriptorError, match="already registered"):
            load_external_agent_plugin_registry(tmp_path)
