"""Multi-machine task dispatch.

This package splits the problem in two on purpose:

* :mod:`.dispatcher` is pure: given a fleet snapshot and a task list, produce a
  :class:`DispatchPlan`. No I/O, no asyncio — every edge case is a table test.
* :mod:`.client` is the async HTTP adapter that talks to remote daemons. The
  underlying HTTP call is injected so unit tests never touch sockets.

The separation means policy changes (scoring, tie-breaking) land in one module,
transport changes (httpx → something else) land in the other.
"""

from __future__ import annotations

from maxwell_daemon.fleet.capabilities import (
    CapabilityView,
    FleetAssignment,
    FleetNode,
    FleetRegistryStatus,
    InMemoryFleetCapabilityRegistry,
    NodeCapability,
    NodeDecision,
    NodePolicy,
    NodePolicySummary,
    NodeResourceSnapshot,
    NodeStatusView,
    TailscalePeerStatus,
    TailscaleStatusView,
    parse_tailscale_status_json,
)
from maxwell_daemon.fleet.client import (
    HTTPClientProtocol,
    RemoteDaemonClient,
    RemoteDaemonError,
    RemoteTaskResult,
)
from maxwell_daemon.fleet.dispatcher import (
    Assignment,
    DispatchPlan,
    FleetDispatcher,
    MachineState,
    TaskRequirement,
    score_machine,
)

__all__ = [
    "Assignment",
    "CapabilityView",
    "DispatchPlan",
    "FleetAssignment",
    "FleetDispatcher",
    "FleetNode",
    "FleetRegistryStatus",
    "HTTPClientProtocol",
    "InMemoryFleetCapabilityRegistry",
    "MachineState",
    "NodeCapability",
    "NodeDecision",
    "NodePolicy",
    "NodePolicySummary",
    "NodeResourceSnapshot",
    "NodeStatusView",
    "RemoteDaemonClient",
    "RemoteDaemonError",
    "RemoteTaskResult",
    "TailscalePeerStatus",
    "TailscaleStatusView",
    "TaskRequirement",
    "parse_tailscale_status_json",
    "score_machine",
]
