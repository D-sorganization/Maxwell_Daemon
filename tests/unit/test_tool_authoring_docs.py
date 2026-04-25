"""Tool authoring documentation contract checks."""

from pathlib import Path

COVERAGE_DOC = Path("docs/community/documentation-coverage.md")
GUIDE_DOC = Path("docs/development/tool-authoring-guide.md")
MKDOCS = Path("mkdocs.yml")


def test_tool_authoring_guide_is_discoverable_from_development_nav() -> None:
    mkdocs = MKDOCS.read_text(encoding="utf-8")

    assert "Tool authoring and MCP boundaries: development/tool-authoring-guide.md" in mkdocs


def test_tool_authoring_guide_covers_runtime_contracts() -> None:
    doc = GUIDE_DOC.read_text(encoding="utf-8")

    assert "ToolSpec" in doc
    assert "ToolParam" in doc
    assert "mcp_tool" in doc
    assert "ToolRegistry" in doc
    assert "ToolPolicy" in doc
    assert "ToolInvocationStore" in doc
    assert "HookRunnerProtocol" in doc


def test_tool_authoring_guide_documents_mcp_boundary_without_overstatement() -> None:
    doc = GUIDE_DOC.read_text(encoding="utf-8")
    normalized = " ".join(doc.lower().split())

    assert "is not a public model context protocol server or client transport today" in normalized
    assert "ToolRegistry.to_openai()" in doc
    assert "ToolRegistry.to_anthropic()" in doc
    assert "not currently shipped" in normalized
    assert "an MCP server entry point" in doc
    assert "MCP client transport handling" in doc
    assert "compatibility tests against third-party MCP clients" in doc


def test_tool_authoring_guide_covers_local_test_harness_and_review_gates() -> None:
    doc = GUIDE_DOC.read_text(encoding="utf-8")
    normalized = " ".join(doc.lower().split())

    assert "python -m pytest tests/unit/test_tools_mcp.py -q" in doc
    assert "python -m pytest tests/unit/test_tools_builtins.py -q" in doc
    assert "python -m pytest tests/unit/test_tools_hooks_integration.py -q" in doc
    assert "use `tmp_path` for workspace roots" in normalized
    assert "use injected `bashrunner` callables instead of real shells" in normalized
    assert "policy tests for denial and approval-required paths" in normalized
    assert "redaction tests for arguments and captured output" in normalized
    assert "workspace-boundary tests for any filesystem access" in normalized


def test_documentation_coverage_tracks_development_guide_gate() -> None:
    coverage = COVERAGE_DOC.read_text(encoding="utf-8")
    normalized = " ".join(coverage.lower().split())

    assert "`development/tool-authoring-guide.md`" in coverage
    assert "Development guide" in coverage
    assert "Shipped" in coverage
    assert "current mcp status boundaries" in normalized
    assert "local test harness" in normalized
