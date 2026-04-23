"""Tailscale deployment security documentation contract checks."""

from __future__ import annotations

from pathlib import Path

TAILSCALE_DOC = Path("docs/operations/tailscale.md")
COVERAGE_DOC = Path("docs/community/documentation-coverage.md")
SECURITY_DOC = Path("docs/operations/security.md")


def test_tailscale_doc_covers_fleet_hardening_controls() -> None:
    doc = TAILSCALE_DOC.read_text(encoding="utf-8")

    assert "## Hardening checklist" in doc
    assert "`api.auth_token` or JWT" in doc
    assert "Never expose `/api/v1/tasks`" in doc
    assert "`/api/v1/memory/*`" in doc
    assert "`/api/v1/ssh/*`" in doc


def test_tailscale_doc_includes_least_privilege_policy_example() -> None:
    doc = TAILSCALE_DOC.read_text(encoding="utf-8")

    assert "Tailscale's current policy syntax recommends grants" in doc
    assert "tag:maxwell-coordinator" in doc
    assert "tag:maxwell-worker" in doc
    assert "tcp:8000" in doc
    assert '"tests": [' in doc
    assert '"deny": ["tag:maxwell-worker:8000"]' in doc


def test_tailscale_doc_includes_validation_commands() -> None:
    doc = TAILSCALE_DOC.read_text(encoding="utf-8")

    assert "tailscale status" in doc
    assert "tailscale ping worker-01.tailnet-name.ts.net" in doc
    assert "/api/v1/fleet/capabilities" in doc
    assert "If a laptop can call a worker's `/api/v1/tasks` endpoint" in doc


def test_documentation_coverage_tracks_tailscale_security_gate() -> None:
    coverage = COVERAGE_DOC.read_text(encoding="utf-8")

    assert "`tailscale.md`" in coverage
    assert "Tailscale-specific security guidance is shipped" in coverage
    assert "least-privilege policy guidance" in coverage


def test_security_doc_links_to_tailscale_hardening_guide() -> None:
    doc = SECURITY_DOC.read_text(encoding="utf-8")

    assert "[Tailscale fleet hardening guide](tailscale.md)" in doc
