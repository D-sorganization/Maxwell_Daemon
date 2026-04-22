"""Local plugin descriptor schema, loader, and registry helpers."""

from maxwell_daemon.plugins.external_agents import (
    ExternalAgentPluginDescriptor,
    ExternalAgentPluginRegistry,
    PluginDescriptorError,
    load_external_agent_plugin_descriptor,
    load_external_agent_plugin_registry,
)

__all__ = [
    "ExternalAgentPluginDescriptor",
    "ExternalAgentPluginRegistry",
    "PluginDescriptorError",
    "load_external_agent_plugin_descriptor",
    "load_external_agent_plugin_registry",
]
