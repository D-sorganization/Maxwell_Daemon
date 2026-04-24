from __future__ import annotations
import pytest
from pathlib import Path

class TestSandboxEdgeCases:
    def test_gate_evidence_to_dict(self) -> None:
        from maxwell_daemon.sandbox.policy import GateEvidence
        evidence = GateEvidence(name="timeout", value="300")
        assert evidence.to_dict() == {"name": "timeout", "value": "300"}

    def test_gate_decision_evidence_value_none(self) -> None:
        from maxwell_daemon.sandbox.policy import GateDecision, GateEvidence
        decision = GateDecision(name="test", passed=True, status="passed", command=("echo",), workspace_root="/tmp", cwd="/tmp", evidence=(GateEvidence("foo", "bar"),))
        assert decision.evidence_value("missing") is None

    def test_gate_decision_to_dict(self) -> None:
        from maxwell_daemon.sandbox.policy import GateDecision, GateEvidence
        decision = GateDecision(name="test", passed=True, status="passed", command=("echo", "1"), workspace_root="/tmp", cwd="/tmp", evidence=(GateEvidence("foo", "bar"),))
        d = decision.to_dict()
        assert d["command"] == ["echo", "1"]
        assert d["evidence"] == [{"name": "foo", "value": "bar"}]

    def test_command_policy_not_allowlisted(self) -> None:
        from maxwell_daemon.sandbox.policy import CommandPolicy
        policy = CommandPolicy(allowed_commands=frozenset({"pytest"}))
        allowed, reason = policy.validate(("python", "script.py"))
        assert allowed is False
        assert "not allowlisted" in reason

    def test_command_policy_destructive_arg(self) -> None:
        from maxwell_daemon.sandbox.policy import CommandPolicy
        policy = CommandPolicy(allowed_commands=frozenset({"git"}), destructive_tokens=frozenset({"--force"}))
        allowed, reason = policy.validate(("git", "push", "--force"))
        assert allowed is False
        assert "destructive argument denied" in reason

    def test_env_policy_filter_empty_allowlist(self) -> None:
        from maxwell_daemon.sandbox.policy import EnvPolicy
        policy = EnvPolicy(allowlist=frozenset())
        assert policy.filter({"FOO": "bar"}) == {}

    def test_sandbox_policy_empty_command(self, tmp_path: Path) -> None:
        from maxwell_daemon.sandbox.policy import SandboxPolicy
        policy = SandboxPolicy.for_workspace(tmp_path)
        decision = policy.validate_command([])
        assert decision.passed is False
        assert decision.status == "policy_denied"
        assert decision.evidence_value("reason") == "command must be non-empty"
