"""Gate runtime documentation contract checks."""

from pathlib import Path

DOC = Path("docs/architecture/gate-runtime.md")


def test_gate_runtime_doc_covers_current_cli_surface() -> None:
    doc = DOC.read_text(encoding="utf-8")

    assert "## Current Operator Surface" in doc
    assert "maxwell-daemon gauntlet list" in doc
    assert "maxwell-daemon gauntlet status task-123" in doc
    assert "maxwell-daemon gauntlet retry task-123" in doc
    assert "maxwell-daemon gate waive task-123" in doc
    assert "/api/v1/control-plane/gauntlet" in doc
    assert "task-scoped control-plane" in doc


def test_gate_runtime_doc_keeps_work_item_run_gap_explicit() -> None:
    doc = DOC.read_text(encoding="utf-8")

    assert "work-item-scoped gauntlet runs remain follow-up integration work" in doc


def test_gate_runtime_doc_lists_built_in_critic_profiles() -> None:
    doc = DOC.read_text(encoding="utf-8")

    assert "architecture-critic" in doc
    assert "test-critic" in doc
    assert "security-critic" in doc
    assert "maintainability-critic" in doc
    assert "product-critic" in doc
    assert "release-critic" in doc
    assert "output schema version" in doc
    assert "timeout and retry policy" in doc
