"""AgentLoopBackend — unit tests for tool execution, safety, and loop control.

All tests that involve the Anthropic API use a synchronous mock so we avoid
real network calls. The agent loop itself is synchronous under the hood (it
uses ``client.messages.create``, not the async variant) so we can exercise it
directly without ``asyncio.run`` in most cases.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from conductor.backends.agent_loop import AgentLoopBackend, TOOL_SCHEMAS


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def _make_backend(**kwargs: Any) -> AgentLoopBackend:
    return AgentLoopBackend(**kwargs)


def _make_response(
    *,
    stop_reason: str = "end_turn",
    text: str = "done",
    tool_calls: list[dict[str, Any]] | None = None,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> MagicMock:
    """Build a fake ``anthropic.types.Message``-like object."""
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.model = "claude-sonnet-4-6"
    resp.usage = MagicMock()
    resp.usage.input_tokens = input_tokens
    resp.usage.output_tokens = output_tokens
    # cache_read_input_tokens may not exist on older SDK shapes
    resp.usage.cache_read_input_tokens = 0

    content: list[MagicMock] = []

    if text:
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = text
        content.append(text_block)

    for tc in tool_calls or []:
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = tc["id"]
        tool_block.name = tc["name"]
        tool_block.input = tc["input"]
        content.append(tool_block)

    resp.content = content
    return resp


# ---------------------------------------------------------------------------
# Tool execution — file operations
# ---------------------------------------------------------------------------


class TestReadFile:
    def test_reads_existing_file(self, tmp_path: Path) -> None:
        (tmp_path / "hello.txt").write_text("hello world")
        backend = _make_backend()
        result = backend._execute_tool("read_file", {"path": "hello.txt"}, str(tmp_path))
        assert result == "hello world"

    def test_missing_file_returns_error(self, tmp_path: Path) -> None:
        backend = _make_backend()
        result = backend._execute_tool("read_file", {"path": "missing.txt"}, str(tmp_path))
        assert result.startswith("ERROR:")

    def test_traversal_rejected(self, tmp_path: Path) -> None:
        backend = _make_backend()
        result = backend._execute_tool(
            "read_file", {"path": "../../etc/passwd"}, str(tmp_path)
        )
        assert "ERROR" in result
        assert "traversal" in result.lower() or "escapes" in result.lower()


class TestWriteFile:
    def test_creates_file(self, tmp_path: Path) -> None:
        backend = _make_backend()
        result = backend._execute_tool(
            "write_file", {"path": "new.txt", "content": "abc"}, str(tmp_path)
        )
        assert "Written" in result
        assert (tmp_path / "new.txt").read_text() == "abc"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        backend = _make_backend()
        backend._execute_tool(
            "write_file", {"path": "sub/dir/file.txt", "content": "x"}, str(tmp_path)
        )
        assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "x"

    def test_traversal_rejected(self, tmp_path: Path) -> None:
        backend = _make_backend()
        result = backend._execute_tool(
            "write_file", {"path": "../outside.txt", "content": "x"}, str(tmp_path)
        )
        assert "ERROR" in result


class TestEditFile:
    def test_replaces_unique_occurrence(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("foo bar baz")
        backend = _make_backend()
        result = backend._execute_tool(
            "edit_file",
            {"path": "f.txt", "old_str": "bar", "new_str": "QUX"},
            str(tmp_path),
        )
        assert "Replaced 1" in result
        assert (tmp_path / "f.txt").read_text() == "foo QUX baz"

    def test_error_if_not_found(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("hello")
        backend = _make_backend()
        result = backend._execute_tool(
            "edit_file",
            {"path": "f.txt", "old_str": "MISSING", "new_str": "x"},
            str(tmp_path),
        )
        assert "ERROR" in result
        assert "not found" in result.lower()

    def test_error_if_not_unique(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("dup dup dup")
        backend = _make_backend()
        result = backend._execute_tool(
            "edit_file",
            {"path": "f.txt", "old_str": "dup", "new_str": "x"},
            str(tmp_path),
        )
        assert "ERROR" in result


class TestRunBash:
    def test_runs_simple_command(self, tmp_path: Path) -> None:
        backend = _make_backend()
        result = backend._execute_tool(
            "run_bash", {"command": "echo hello"}, str(tmp_path)
        )
        assert "hello" in result

    def test_nonzero_exit_includes_output(self, tmp_path: Path) -> None:
        backend = _make_backend()
        result = backend._execute_tool(
            "run_bash", {"command": "exit 42"}, str(tmp_path)
        )
        assert "42" in result

    def test_runs_in_workspace_dir(self, tmp_path: Path) -> None:
        (tmp_path / "marker.txt").write_text("found")
        backend = _make_backend()
        result = backend._execute_tool(
            "run_bash", {"command": "cat marker.txt"}, str(tmp_path)
        )
        assert "found" in result


class TestGlobFiles:
    def test_finds_matching_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")
        backend = _make_backend()
        result = backend._execute_tool(
            "glob_files", {"pattern": "*.py"}, str(tmp_path)
        )
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result

    def test_no_matches(self, tmp_path: Path) -> None:
        backend = _make_backend()
        result = backend._execute_tool(
            "glob_files", {"pattern": "*.xyz"}, str(tmp_path)
        )
        assert "no matches" in result.lower()

    def test_recursive_glob(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text("")
        backend = _make_backend()
        result = backend._execute_tool(
            "glob_files", {"pattern": "**/*.py"}, str(tmp_path)
        )
        assert "deep.py" in result


class TestGrepFiles:
    def test_finds_pattern(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text("def my_func():\n    pass\n")
        backend = _make_backend()
        result = backend._execute_tool(
            "grep_files", {"pattern": "my_func"}, str(tmp_path)
        )
        assert "my_func" in result

    def test_no_match_returns_no_matches(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text("hello world")
        backend = _make_backend()
        result = backend._execute_tool(
            "grep_files", {"pattern": "XXXX_NOTHERE"}, str(tmp_path)
        )
        assert "no matches" in result.lower()

    def test_traversal_in_search_path_rejected(self, tmp_path: Path) -> None:
        backend = _make_backend()
        result = backend._execute_tool(
            "grep_files", {"pattern": "x", "path": "../../"}, str(tmp_path)
        )
        assert "ERROR" in result


# ---------------------------------------------------------------------------
# Path traversal safety
# ---------------------------------------------------------------------------


class TestPathTraversal:
    @pytest.mark.parametrize(
        "bad_path",
        [
            "../../etc/passwd",
            "../outside",
            "/etc/passwd",
            "/tmp/evil",
        ],
    )
    def test_read_rejects_escaping_paths(self, bad_path: str, tmp_path: Path) -> None:
        backend = _make_backend()
        result = backend._execute_tool(
            "read_file", {"path": bad_path}, str(tmp_path)
        )
        assert "ERROR" in result

    @pytest.mark.parametrize(
        "bad_path",
        [
            "../../evil.txt",
            "/tmp/injected.txt",
        ],
    )
    def test_write_rejects_escaping_paths(self, bad_path: str, tmp_path: Path) -> None:
        backend = _make_backend()
        result = backend._execute_tool(
            "write_file", {"path": bad_path, "content": "pwned"}, str(tmp_path)
        )
        assert "ERROR" in result


# ---------------------------------------------------------------------------
# Agent loop — turn limit enforcement
# ---------------------------------------------------------------------------


class TestTurnLimit:
    def test_raises_on_turn_limit_exceeded(self, tmp_path: Path) -> None:
        """When every response is tool_use, the loop must raise after max_turns."""
        backend = _make_backend(max_turns=3)

        # Every response asks for a tool call that never ends.
        tool_response = _make_response(
            stop_reason="tool_use",
            text="",
            tool_calls=[
                {
                    "id": "tu_1",
                    "name": "run_bash",
                    "input": {"command": "echo hi"},
                }
            ],
        )

        with patch.object(backend._client.messages, "create", return_value=tool_response):
            with pytest.raises(RuntimeError, match="max_turns=3"):
                asyncio.run(
                    backend.complete(
                        [],
                        workspace_dir=str(tmp_path),
                    )
                )

    def test_max_turns_override_per_call(self, tmp_path: Path) -> None:
        backend = _make_backend(max_turns=150)  # default high
        tool_response = _make_response(
            stop_reason="tool_use",
            text="",
            tool_calls=[
                {
                    "id": "tu_1",
                    "name": "run_bash",
                    "input": {"command": "echo hi"},
                }
            ],
        )
        with patch.object(backend._client.messages, "create", return_value=tool_response):
            with pytest.raises(RuntimeError, match="max_turns=2"):
                asyncio.run(
                    backend.complete(
                        [],
                        workspace_dir=str(tmp_path),
                        max_turns=2,
                    )
                )


# ---------------------------------------------------------------------------
# Agent loop — end_turn exits cleanly
# ---------------------------------------------------------------------------


class TestEndTurnExits:
    def test_single_turn_end_turn(self, tmp_path: Path) -> None:
        backend = _make_backend()
        final_response = _make_response(stop_reason="end_turn", text="All done!")

        with patch.object(backend._client.messages, "create", return_value=final_response):
            resp = asyncio.run(
                backend.complete(
                    [],
                    workspace_dir=str(tmp_path),
                )
            )
        assert resp.content == "All done!"
        assert resp.finish_reason == "end_turn"
        assert resp.backend == "agent-loop"

    def test_tool_then_end_turn(self, tmp_path: Path) -> None:
        """Two-turn sequence: tool_use → end_turn."""
        (tmp_path / "note.txt").write_text("agent result")
        backend = _make_backend()

        turn1 = _make_response(
            stop_reason="tool_use",
            text="Reading the file...",
            tool_calls=[
                {
                    "id": "tu_read",
                    "name": "read_file",
                    "input": {"path": "note.txt"},
                }
            ],
        )
        turn2 = _make_response(stop_reason="end_turn", text="File says: agent result")

        call_count = 0

        def _side_effect(**kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            return turn1 if call_count == 1 else turn2

        with patch.object(backend._client.messages, "create", side_effect=_side_effect):
            resp = asyncio.run(
                backend.complete(
                    [],
                    workspace_dir=str(tmp_path),
                )
            )

        assert resp.content == "File says: agent result"
        assert call_count == 2

    def test_token_usage_accumulated_across_turns(self, tmp_path: Path) -> None:
        backend = _make_backend()

        turn1 = _make_response(
            stop_reason="tool_use",
            text="",
            tool_calls=[{"id": "tu1", "name": "run_bash", "input": {"command": "echo x"}}],
            input_tokens=100,
            output_tokens=50,
        )
        turn2 = _make_response(
            stop_reason="end_turn", text="done", input_tokens=200, output_tokens=80
        )

        call_n = 0

        def _side(**kw: Any) -> MagicMock:
            nonlocal call_n
            call_n += 1
            return turn1 if call_n == 1 else turn2

        with patch.object(backend._client.messages, "create", side_effect=_side):
            resp = asyncio.run(backend.complete([], workspace_dir=str(tmp_path)))

        assert resp.usage.prompt_tokens == 300
        assert resp.usage.completion_tokens == 130


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------


class TestToolSchemas:
    def test_all_tools_present(self) -> None:
        names = {t["name"] for t in TOOL_SCHEMAS}
        assert names == {
            "read_file",
            "write_file",
            "edit_file",
            "run_bash",
            "glob_files",
            "grep_files",
        }

    def test_each_has_input_schema(self) -> None:
        for tool in TOOL_SCHEMAS:
            assert "input_schema" in tool, f"{tool['name']} missing input_schema"
            assert "properties" in tool["input_schema"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_registered_as_agent_loop(self) -> None:
        from conductor.backends.registry import registry

        assert "agent-loop" in registry.available()

    def test_capabilities_marks_tool_use(self) -> None:
        backend = _make_backend()
        caps = backend.capabilities("claude-sonnet-4-6")
        assert caps.supports_tool_use is True
        assert caps.is_local is False

    def test_pricing_populated(self) -> None:
        backend = _make_backend()
        caps = backend.capabilities("claude-sonnet-4-6")
        assert caps.cost_per_1k_input_tokens > 0
        assert caps.cost_per_1k_output_tokens > 0
