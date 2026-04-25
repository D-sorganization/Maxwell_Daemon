"""Unit tests for the fleet capability registry and Tailscale-aware selection."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from maxwell_daemon.fleet.capabilities import (
    FleetAssignment,
    FleetNode,
    InMemoryFleetCapabilityRegistry,
    NodeCapability,
    NodePolicy,
    NodeResourceSnapshot,
    TailscalePeerStatus,
    parse_tailscale_status_json,
)


def _dt(minutes_ago: int = 0) -> datetime:
    return datetime(2026, 4, 22, 18, 0, tzinfo=timezone.utc) - timedelta(
        minutes=minutes_ago
    )


def _capability(name: str, minutes_ago: int = 0) -> NodeCapability:
    return NodeCapability(name=name, observed_at=_dt(minutes_ago))


def _snapshot(*, minutes_ago: int = 0, sessions: int = 0) -> NodeResourceSnapshot:
    captured = _dt(minutes_ago)
    return NodeResourceSnapshot(
        captured_at=captured, heartbeat_at=captured, active_sessions=sessions
    )


def _policy(
    *,
    repos: frozenset[str] | None = None,
    tools: frozenset[str] | None = None,
    max_sessions: int = 2,
    stale_after_seconds: int = 600,
) -> NodePolicy:
    return NodePolicy(
        allowed_repos=repos,
        allowed_tools=tools,
        max_concurrent_sessions=max_sessions,
        heartbeat_stale_after_seconds=stale_after_seconds,
    )


def _node(
    node_id: str,
    *,
    hostname: str | None = None,
    capabilities: tuple[NodeCapability, ...] = (),
    minutes_ago: int = 0,
    sessions: int = 0,
    repos: frozenset[str] | None = None,
    tools: frozenset[str] | None = None,
    max_sessions: int = 2,
    stale_after_seconds: int = 600,
    tailscale_online: bool = True,
) -> FleetNode:
    status = TailscalePeerStatus(
        peer_id=node_id,
        hostname=hostname or node_id,
        online=tailscale_online,
        tailnet_ip=f"100.64.0.{len(node_id) + 10}",
        current_address=f"100.64.0.{len(node_id) + 10}:41641",
        last_seen_at=_dt(minutes_ago),
    )
    return FleetNode(
        node_id=node_id,
        hostname=hostname or node_id,
        capabilities=capabilities,
        resource_snapshot=_snapshot(minutes_ago=minutes_ago, sessions=sessions),
        policy=_policy(
            repos=repos,
            tools=tools,
            max_sessions=max_sessions,
            stale_after_seconds=stale_after_seconds,
        ),
        tailscale_status=status,
    )


def test_tailscale_parser_handles_reachable_and_offline_peers() -> None:
    raw = json.dumps(
        {
            "Peer": {
                "100.64.0.10": {
                    "HostName": "alpha",
                    "Online": True,
                    "TailscaleIPs": ["100.64.0.10"],
                    "CurAddr": "100.64.0.10:41641",
                    "LastSeen": "2026-04-22T17:58:00Z",
                },
                "100.64.0.11": {
                    "HostName": "beta",
                    "Online": False,
                    "TailscaleIPs": ["100.64.0.11"],
                    "CurAddr": "100.64.0.11:41641",
                    "LastSeen": "2026-04-22T17:50:00Z",
                },
            }
        }
    )

    peers = parse_tailscale_status_json(raw)
    assert peers == (
        TailscalePeerStatus(
            peer_id="100.64.0.10",
            hostname="alpha",
            online=True,
            tailnet_ip="100.64.0.10",
            current_address="100.64.0.10:41641",
            last_seen_at=datetime(2026, 4, 22, 17, 58, tzinfo=timezone.utc),
        ),
        TailscalePeerStatus(
            peer_id="100.64.0.11",
            hostname="beta",
            online=False,
            tailnet_ip="100.64.0.11",
            current_address="100.64.0.11:41641",
            last_seen_at=datetime(2026, 4, 22, 17, 50, tzinfo=timezone.utc),
        ),
    )


def test_tailscale_parser_handles_peer_list_payloads() -> None:
    raw = {
        "Peers": [
            {
                "ID": "100.64.0.11",
                "hostname": "beta",
                "online": False,
                "tailnet_ip": "100.64.0.11",
                "current_address": "100.64.0.11:41641",
                "last_seen": "2026-04-22T17:50:00Z",
            },
            {
                "ID": "100.64.0.10",
                "hostname": "alpha",
                "online": True,
                "tailnet_ip": "100.64.0.10",
                "current_address": "100.64.0.10:41641",
                "last_seen": "2026-04-22T17:58:00Z",
            },
        ]
    }

    peers = parse_tailscale_status_json(raw)
    assert [peer.peer_id for peer in peers] == ["100.64.0.10", "100.64.0.11"]
    assert peers[0].hostname == "alpha"
    assert peers[1].online is False


def test_registry_selects_best_node_and_explains_rejections() -> None:
    registry = InMemoryFleetCapabilityRegistry(
        (
            _node(
                "node-a",
                hostname="alpha",
                capabilities=(
                    _capability("gpu", minutes_ago=5),
                    _capability("tailscale", minutes_ago=1),
                ),
                sessions=0,
                repos=frozenset({"acme/repo"}),
                tools=frozenset({"dispatch"}),
            ),
            _node(
                "node-b",
                hostname="beta",
                capabilities=(_capability("gpu"),),
                sessions=0,
                repos=frozenset({"acme/repo"}),
                tools=frozenset({"dispatch"}),
                tailscale_online=False,
            ),
            _node(
                "node-c",
                hostname="gamma",
                capabilities=(_capability("cpu"),),
                sessions=0,
                repos=frozenset({"acme/repo"}),
                tools=frozenset({"dispatch"}),
            ),
        )
    )

    assignment = registry.select(
        repo="acme/repo",
        tool="dispatch",
        required_capabilities=("gpu",),
        now=_dt(),
    )

    assert isinstance(assignment, FleetAssignment)
    assert assignment.selected_node is not None
    assert assignment.selected_node.node_id == "node-a"
    assert assignment.selected_node.capabilities[0].observed_at == _dt(5)
    rejection_text = " ".join(
        reason for decision in assignment.rejected_nodes for reason in decision.reasons
    )
    assert "tailscale peer offline" in rejection_text
    assert "missing capabilities: 'gpu'" in rejection_text
    assert "selected 'alpha'" in assignment.explanation
    assert "beta" in assignment.explanation and "gamma" in assignment.explanation


def test_offline_and_stale_nodes_are_not_selected() -> None:
    registry = InMemoryFleetCapabilityRegistry(
        (
            _node(
                "online",
                hostname="online",
                capabilities=(_capability("gpu"),),
                minutes_ago=2,
                sessions=0,
            ),
            _node(
                "offline",
                hostname="offline",
                capabilities=(_capability("gpu"),),
                minutes_ago=2,
                sessions=0,
                tailscale_online=False,
            ),
            _node(
                "stale",
                hostname="stale",
                capabilities=(_capability("gpu"),),
                minutes_ago=20,
                sessions=0,
                stale_after_seconds=60,
            ),
        )
    )

    assignment = registry.select(
        repo="acme/repo", tool="dispatch", required_capabilities=("gpu",), now=_dt()
    )

    assert assignment.selected_node is not None
    assert assignment.selected_node.node_id == "online"
    reasons = {
        decision.node_id: decision.reasons for decision in assignment.rejected_nodes
    }
    assert "tailscale peer offline" in reasons["offline"]
    assert any("heartbeat stale" in reason for reason in reasons["stale"])


def test_missing_capabilities_reject() -> None:
    registry = InMemoryFleetCapabilityRegistry(
        (
            _node(
                "node-a",
                capabilities=(_capability("cpu"),),
                repos=frozenset({"acme/repo"}),
                tools=frozenset({"dispatch"}),
            ),
        )
    )

    assignment = registry.select(
        repo="acme/repo", tool="dispatch", required_capabilities=("gpu",), now=_dt()
    )

    assert assignment.selected_node is None
    assert assignment.rejected_nodes[0].reasons == ("missing capabilities: 'gpu'",)


def test_policy_denies_repo_and_tool() -> None:
    registry = InMemoryFleetCapabilityRegistry(
        (
            _node(
                "node-a",
                capabilities=(_capability("gpu"),),
                repos=frozenset({"acme/other"}),
                tools=frozenset({"lint"}),
            ),
        )
    )

    assignment = registry.select(
        repo="acme/repo", tool="dispatch", required_capabilities=("gpu",), now=_dt()
    )

    assert assignment.selected_node is None
    assert "repo 'acme/repo' not allowed" in assignment.rejected_nodes[0].reasons
    assert "tool 'dispatch' not allowed" in assignment.rejected_nodes[0].reasons


def test_max_concurrent_sessions_enforced() -> None:
    registry = InMemoryFleetCapabilityRegistry(
        (
            _node(
                "full",
                capabilities=(_capability("gpu"),),
                sessions=2,
                max_sessions=2,
            ),
            _node(
                "open",
                capabilities=(_capability("gpu"),),
                sessions=0,
                max_sessions=2,
            ),
        )
    )

    assignment = registry.select(
        repo="acme/repo", tool="dispatch", required_capabilities=("gpu",), now=_dt()
    )

    assert assignment.selected_node is not None
    assert assignment.selected_node.node_id == "open"
    reasons = {
        decision.node_id: decision.reasons for decision in assignment.rejected_nodes
    }
    assert reasons["full"] == ("max concurrent sessions reached (2/2)",)


def test_capability_timestamps_preserved() -> None:
    capability_at = datetime(2026, 4, 22, 17, 42, tzinfo=timezone.utc)
    node = FleetNode(
        node_id="node-a",
        hostname="alpha",
        capabilities=(NodeCapability(name="gpu", observed_at=capability_at),),
        resource_snapshot=NodeResourceSnapshot(
            captured_at=datetime(2026, 4, 22, 17, 59, tzinfo=timezone.utc),
            heartbeat_at=datetime(2026, 4, 22, 17, 59, tzinfo=timezone.utc),
            active_sessions=0,
        ),
        policy=NodePolicy(
            allowed_repos=frozenset({"acme/repo"}),
            allowed_tools=frozenset({"dispatch"}),
            max_concurrent_sessions=1,
            heartbeat_stale_after_seconds=600,
        ),
    )
    registry = InMemoryFleetCapabilityRegistry((node,))

    assignment = registry.select(
        repo="acme/repo", tool="dispatch", required_capabilities=("gpu",), now=_dt()
    )

    assert assignment.selected_node is not None
    assert assignment.selected_node.capabilities[0].observed_at == capability_at


def test_eligible_nodes_return_reasons_and_scores() -> None:
    registry = InMemoryFleetCapabilityRegistry(
        (
            _node(
                "good",
                hostname="good",
                capabilities=(_capability("gpu"),),
                minutes_ago=1,
                sessions=0,
            ),
            _node(
                "bad",
                hostname="bad",
                capabilities=(_capability("cpu"),),
                minutes_ago=1,
                sessions=0,
            ),
        )
    )

    decisions = registry.eligible_nodes(
        repo="acme/repo",
        tool="dispatch",
        required_capabilities=("gpu",),
        now=_dt(),
    )

    by_id = {decision.node_id: decision for decision in decisions}
    assert by_id["good"].score is not None
    assert by_id["good"].eligible is True
    assert by_id["bad"].score is None
    assert by_id["bad"].reasons == ("missing capabilities: 'gpu'",)


def test_registry_operations_update_node_state() -> None:
    registry = InMemoryFleetCapabilityRegistry()
    node = _node(
        "node-a",
        hostname="alpha",
        capabilities=(_capability("cpu"),),
        minutes_ago=4,
        sessions=0,
        repos=frozenset({"acme/repo"}),
        tools=frozenset({"dispatch"}),
    )

    registry.register(node)
    registry.update_capabilities(
        "node-a",
        (
            _capability("gpu"),
            _capability("tailscale"),
        ),
    )
    registry.heartbeat(
        "node-a",
        NodeResourceSnapshot(
            captured_at=_dt(),
            heartbeat_at=_dt(),
            active_sessions=1,
        ),
    )
    registry.mark_offline("node-a")

    updated = registry.list_nodes()[0]
    assert updated.capabilities[0].name == "gpu"
    assert updated.resource_snapshot.active_sessions == 1
    assert updated.tailscale_status is not None
    assert updated.tailscale_status.online is False


def test_registry_status_redacts_policy_and_capability_values() -> None:
    registry = InMemoryFleetCapabilityRegistry(
        (
            _node(
                "node-a",
                hostname="alpha",
                capabilities=(
                    NodeCapability(name="gpu", observed_at=_dt(), value=8),
                    NodeCapability(
                        name="secret-capability", observed_at=_dt(), value="hidden"
                    ),
                ),
                sessions=0,
                repos=frozenset({"acme/repo"}),
                tools=frozenset({"dispatch"}),
            ),
        )
    )

    status = registry.describe(
        repo="acme/repo",
        tool="dispatch",
        required_capabilities=("gpu",),
        now=_dt(),
    )

    payload = status.to_dict()
    node = payload["nodes"][0]
    assert node["policy"] == {
        "has_repo_allowlist": True,
        "has_tool_allowlist": True,
        "allowed_repo_count": 1,
        "allowed_tool_count": 1,
        "max_concurrent_sessions": 2,
        "heartbeat_stale_after_seconds": 600,
    }
    assert node["capability_names"] == ["gpu", "secret-capability"]
    assert node["capabilities"][0] == {
        "name": "gpu",
        "observed_at": _dt().isoformat(),
        "has_value": True,
    }
    assert "allowed_repos" not in node["policy"]
    assert "value" not in payload
    assert payload["selected_node"]["hostname"] == "alpha"
    assert payload["selected_node"]["tailscale_status"] is not None


def test_frozen_dataclasses_stay_frozen() -> None:
    node = _node("node-a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.hostname = "other"  # type: ignore[misc]
