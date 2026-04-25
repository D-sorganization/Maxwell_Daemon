"""Tests for local external-agent plugin descriptor loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from maxwell_daemon.backends.external_adapter import (
    ExternalAgentAdapterBase,
    ExternalAgentAdapterError,
    ExternalAgentAdapterRegistry,
    ExternalAgentCapability,
    ExternalAgentOperation,
    ExternalAgentProbeResult,
    ExternalAgentProbeSpec,
    ExternalAgentRunContext,
    ExternalAgentRunResult,
)
from maxwell_daemon.backends.external_plugins import (
    ExternalAgentPluginDescriptor,
    load_external_agent_adapter,
    load_external_agent_plugin_descriptor,
    load_external_agent_plugin_descriptors,
    register_external_agent_plugins,
)


class DescriptorAdapter(ExternalAgentAdapterBase):
    adapter_id = "descriptor-agent"
    capabilities = ExternalAgentCapability(
        adapter_id=adapter_id,
        display_name="Descriptor Agent",
        supported_operations=frozenset({ExternalAgentOperation.PLAN}),
        read_only_operations=frozenset({ExternalAgentOperation.PLAN}),
        write_operations=frozenset(),
        capability_tags=frozenset({"plan"}),
    )

    def _probe(self, spec: ExternalAgentProbeSpec) -> ExternalAgentProbeResult:
        _ = spec
        return ExternalAgentProbeResult(adapter_id=self.adapter_id, summary="ready")

    def _run(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult:
        return ExternalAgentRunResult.completed(
            adapter_id=self.adapter_id,
            operation=context.operation,
            summary="planned",
        )


class MismatchedAdapter(DescriptorAdapter):
    adapter_id = "other-agent"
    capabilities = ExternalAgentCapability(
        adapter_id=adapter_id,
        supported_operations=frozenset({ExternalAgentOperation.PLAN}),
    )


def _descriptor_payload(entrypoint: str | None = None) -> dict[str, object]:
    return {
        "name": "descriptor-agent",
        "kind": "external-agent",
        "entrypoint": entrypoint or "tests.unit.test_external_agent_plugins:DescriptorAdapter",
        "version": "1",
        "capabilities": ["plan", "stdout-artifacts"],
    }


def test_json_descriptor_loads_valid_metadata(tmp_path: Path) -> None:
    path = tmp_path / "descriptor-agent.json"
    path.write_text(json.dumps(_descriptor_payload()), encoding="utf-8")

    descriptor = load_external_agent_plugin_descriptor(path)

    assert descriptor == ExternalAgentPluginDescriptor(
        name="descriptor-agent",
        kind="external-agent",
        entrypoint="tests.unit.test_external_agent_plugins:DescriptorAdapter",
        version="1",
        capabilities=("plan", "stdout-artifacts"),
    )


def test_toml_descriptor_loads_valid_metadata(tmp_path: Path) -> None:
    path = tmp_path / "descriptor-agent.toml"
    path.write_text(
        "\n".join(
            [
                'name = "descriptor-agent"',
                'kind = "external-agent"',
                'entrypoint = "tests.unit.test_external_agent_plugins:DescriptorAdapter"',
                'version = "1"',
                'capabilities = ["plan", "stdout-artifacts"]',
            ]
        ),
        encoding="utf-8",
    )

    descriptor = load_external_agent_plugin_descriptor(path)

    assert descriptor.name == "descriptor-agent"
    assert descriptor.capabilities == ("plan", "stdout-artifacts")


def test_descriptor_validation_rejects_invalid_kind_and_entrypoint() -> None:
    with pytest.raises(ExternalAgentAdapterError, match="kind"):
        ExternalAgentPluginDescriptor(
            name="descriptor-agent",
            kind="tool",
            entrypoint="tests.unit.test_external_agent_plugins:DescriptorAdapter",
        )

    with pytest.raises(ExternalAgentAdapterError, match="entrypoint"):
        ExternalAgentPluginDescriptor(
            name="descriptor-agent",
            kind="external-agent",
            entrypoint="not-a-module",
        )


def test_descriptor_validation_rejects_duplicate_capabilities() -> None:
    with pytest.raises(ExternalAgentAdapterError, match="duplicates"):
        ExternalAgentPluginDescriptor(
            name="descriptor-agent",
            kind="external-agent",
            entrypoint="tests.unit.test_external_agent_plugins:DescriptorAdapter",
            capabilities=("plan", "plan"),
        )


def test_directory_loader_rejects_duplicate_plugin_names(tmp_path: Path) -> None:
    for filename in ("a.json", "b.json"):
        (tmp_path / filename).write_text(json.dumps(_descriptor_payload()), encoding="utf-8")

    with pytest.raises(ExternalAgentAdapterError, match="duplicate"):
        load_external_agent_plugin_descriptors(tmp_path)


def test_load_adapter_from_descriptor_entrypoint() -> None:
    descriptor = ExternalAgentPluginDescriptor(
        name="descriptor-agent",
        kind="external-agent",
        entrypoint="tests.unit.test_external_agent_plugins:DescriptorAdapter",
    )

    adapter = load_external_agent_adapter(descriptor)

    assert adapter.adapter_id == "descriptor-agent"
    assert adapter.probe().available is True


def test_register_external_agent_plugins_adds_adapters_to_registry() -> None:
    registry = ExternalAgentAdapterRegistry()
    descriptor = ExternalAgentPluginDescriptor(
        name="descriptor-agent",
        kind="external-agent",
        entrypoint="tests.unit.test_external_agent_plugins:DescriptorAdapter",
    )

    registered = register_external_agent_plugins(registry, (descriptor,))

    assert registered == ("descriptor-agent",)
    assert (
        registry.resolve("descriptor-agent")
        .run(
            ExternalAgentRunContext(
                adapter_id="descriptor-agent",
                operation=ExternalAgentOperation.PLAN,
                prompt="plan",
            )
        )
        .summary
        == "planned"
    )


def test_adapter_id_mismatch_is_rejected() -> None:
    descriptor = ExternalAgentPluginDescriptor(
        name="descriptor-agent",
        kind="external-agent",
        entrypoint="tests.unit.test_external_agent_plugins:MismatchedAdapter",
    )

    with pytest.raises(ExternalAgentAdapterError, match="adapter id mismatch"):
        load_external_agent_adapter(descriptor)


def test_unsupported_descriptor_suffix_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "descriptor-agent.yaml"
    path.write_text("name: descriptor-agent", encoding="utf-8")

    with pytest.raises(ExternalAgentAdapterError, match="unsupported"):
        load_external_agent_plugin_descriptor(path)
