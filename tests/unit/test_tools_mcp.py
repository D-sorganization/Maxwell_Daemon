"""Unit tests for maxwell_daemon.tools.mcp — the Model Context Protocol abstraction.

The abstraction exists so tool *definitions* live in one place (a ``ToolSpec``)
and *emit* provider-specific schemas (Anthropic, OpenAI) on demand. Handlers
run through the registry so every backend uses the same implementations.
"""

from __future__ import annotations

import pytest

from maxwell_daemon.tools.mcp import (
    ToolInvocationStore,
    ToolParam,
    ToolPolicy,
    ToolRegistry,
    ToolRegistryError,
    ToolResult,
    ToolSpec,
    mcp_tool,
)

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


def _echo_handler(message: str) -> str:
    return f"echo: {message}"


async def _async_echo_handler(message: str) -> str:
    return f"async-echo: {message}"


def _echo_spec() -> ToolSpec:
    return ToolSpec(
        name="echo",
        description="Return the message prefixed with 'echo:'",
        params=[ToolParam(name="message", type="string", description="Input text")],
        handler=_echo_handler,
    )


class TestToolSpecAnthropicSchema:
    """ToolSpec.to_anthropic must match Anthropic tool_use shape exactly."""

    def test_basic_string_param(self) -> None:
        schema = _echo_spec().to_anthropic()
        assert schema["name"] == "echo"
        assert schema["description"] == "Return the message prefixed with 'echo:'"
        assert schema["input_schema"]["type"] == "object"
        assert schema["input_schema"]["properties"] == {
            "message": {"type": "string", "description": "Input text"},
        }
        assert schema["input_schema"]["required"] == ["message"]

    def test_optional_param_not_in_required(self) -> None:
        spec = ToolSpec(
            name="t",
            description="desc",
            params=[
                ToolParam(name="a", type="string", description="required"),
                ToolParam(
                    name="b", type="integer", description="optional", required=False
                ),
            ],
            handler=lambda **_: "x",
        )
        schema = spec.to_anthropic()
        assert schema["input_schema"]["required"] == ["a"]
        assert set(schema["input_schema"]["properties"].keys()) == {"a", "b"}

    def test_enum_field_is_emitted(self) -> None:
        spec = ToolSpec(
            name="t",
            description="desc",
            params=[
                ToolParam(
                    name="color", type="string", description="c", enum=["red", "blue"]
                ),
            ],
            handler=lambda **_: "x",
        )
        schema = spec.to_anthropic()
        assert schema["input_schema"]["properties"]["color"]["enum"] == ["red", "blue"]


class TestToolSpecOpenAISchema:
    """ToolSpec.to_openai must match OpenAI function-calling shape exactly."""

    def test_basic(self) -> None:
        schema = _echo_spec().to_openai()
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == "echo"
        assert fn["description"] == "Return the message prefixed with 'echo:'"
        assert fn["parameters"]["type"] == "object"
        assert fn["parameters"]["required"] == ["message"]


class TestToolRegistry:
    def test_register_and_get(self) -> None:
        reg = ToolRegistry()
        reg.register(_echo_spec())
        assert reg.get("echo").name == "echo"

    def test_register_duplicate_rejected(self) -> None:
        reg = ToolRegistry()
        reg.register(_echo_spec())
        with pytest.raises(ToolRegistryError, match="already registered"):
            reg.register(_echo_spec())

    def test_get_unknown_rejected(self) -> None:
        reg = ToolRegistry()
        with pytest.raises(ToolRegistryError, match="unknown tool"):
            reg.get("nope")

    def test_names_sorted(self) -> None:
        reg = ToolRegistry()
        reg.register(
            ToolSpec(name="zulu", description="z", params=[], handler=lambda: "z")
        )
        reg.register(
            ToolSpec(name="alpha", description="a", params=[], handler=lambda: "a")
        )
        assert reg.names() == ["alpha", "zulu"]

    def test_to_anthropic_returns_list(self) -> None:
        reg = ToolRegistry()
        reg.register(_echo_spec())
        schemas = reg.to_anthropic()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "echo"

    def test_to_openai_returns_list(self) -> None:
        reg = ToolRegistry()
        reg.register(_echo_spec())
        schemas = reg.to_openai()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "echo"


class TestToolRegistryInvoke:
    async def test_invokes_sync_handler(self) -> None:
        reg = ToolRegistry()
        reg.register(_echo_spec())
        result = await reg.invoke("echo", {"message": "hi"})
        assert result == ToolResult(content="echo: hi", is_error=False)

    async def test_invokes_async_handler(self) -> None:
        reg = ToolRegistry()
        reg.register(
            ToolSpec(
                name="aecho",
                description="async echo",
                params=[ToolParam(name="message", type="string", description="m")],
                handler=_async_echo_handler,
            )
        )
        result = await reg.invoke("aecho", {"message": "hi"})
        assert result == ToolResult(content="async-echo: hi", is_error=False)

    async def test_handler_exception_returns_error_result(self) -> None:
        def boom(**_: object) -> str:
            raise ValueError("kaboom")

        reg = ToolRegistry()
        reg.register(
            ToolSpec(name="boom", description="fails", params=[], handler=boom),
        )
        result = await reg.invoke("boom", {})
        assert result.is_error is True
        assert "ValueError" in result.content
        assert "kaboom" in result.content

    async def test_unknown_tool_raises(self) -> None:
        reg = ToolRegistry()
        with pytest.raises(ToolRegistryError, match="unknown tool"):
            await reg.invoke("nope", {})


class TestApprovalTierEnforcement:
    """Issue #237: approval tiers must be enforced before running tool handlers."""

    async def test_suggest_tier_blocks_execution(self) -> None:
        """'suggest' tier must return an error result without running the handler."""
        ran: list[bool] = []

        def _side_effect(**_: object) -> str:
            ran.append(True)
            return "should not run"

        reg = ToolRegistry(approval_tier="suggest")
        reg.register(
            ToolSpec(name="t", description="d", params=[], handler=_side_effect)
        )
        result = await reg.invoke("t", {})
        assert result.is_error is True
        assert "approval" in result.content.lower()
        assert ran == [], "handler must not execute under 'suggest' tier"

    async def test_full_auto_tier_allows_execution(self) -> None:
        reg = ToolRegistry(approval_tier="full-auto")
        reg.register(_echo_spec())
        result = await reg.invoke("echo", {"message": "hi"})
        assert result.is_error is False
        assert result.content == "echo: hi"

    async def test_auto_edit_tier_allows_execution(self) -> None:
        """'auto-edit' is a supervised-edit tier but still permits execution."""
        reg = ToolRegistry(approval_tier="auto-edit")
        reg.register(_echo_spec())
        result = await reg.invoke("echo", {"message": "hi"})
        assert result.is_error is False
        assert result.content == "echo: hi"

    async def test_default_tier_is_full_auto(self) -> None:
        """Default ToolRegistry construction must not block any tool invocation."""
        reg = ToolRegistry()
        reg.register(_echo_spec())
        result = await reg.invoke("echo", {"message": "default"})
        assert result.is_error is False

    async def test_suggest_tier_error_message_names_tool(self) -> None:
        reg = ToolRegistry(approval_tier="suggest")
        reg.register(_echo_spec())
        result = await reg.invoke("echo", {"message": "x"})
        assert "echo" in result.content


class TestToolPolicyAndInvocationAudit:
    async def test_readonly_policy_allows_read_tool_and_records_success(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        store = ToolInvocationStore(tmp_path / "tool-invocations.jsonl")
        reg = ToolRegistry(
            policy=ToolPolicy.readonly_default(),
            invocation_store=store,
        )
        reg.register(
            ToolSpec(
                name="read_file",
                description="read",
                params=[ToolParam(name="path", type="string", description="path")],
                handler=lambda path, **_: f"read {path}",
                capabilities=frozenset({"file_read"}),
                risk_level="read_only",
            )
        )

        result = await reg.invoke(
            "read_file", {"path": "README.md", "token": "secret-token"}
        )

        assert result == ToolResult(content="read README.md", is_error=False)
        [record] = store.records
        assert record.tool_name == "read_file"
        assert record.status == "succeeded"
        assert record.redacted_arguments == {"path": "README.md", "token": "***"}
        assert "secret-token" not in (tmp_path / "tool-invocations.jsonl").read_text()

    async def test_policy_denies_unallowed_capability_before_handler_runs(self) -> None:
        ran: list[bool] = []
        store = ToolInvocationStore()
        reg = ToolRegistry(
            policy=ToolPolicy.readonly_default(),
            invocation_store=store,
        )
        reg.register(
            ToolSpec(
                name="write_file",
                description="write",
                params=[],
                handler=lambda: ran.append(True),
                capabilities=frozenset({"file_write"}),
                risk_level="local_write",
            )
        )

        result = await reg.invoke("write_file", {"path": "out.txt"})

        assert result.is_error is True
        assert "denied by policy" in result.content
        assert ran == []
        [record] = store.records
        assert record.status == "denied"
        assert "unallowed capabilities" in (record.error or "")

    async def test_denied_tool_id_wins_over_allowed_capability(self) -> None:
        store = ToolInvocationStore()
        reg = ToolRegistry(
            policy=ToolPolicy(
                denied_tool_ids=frozenset({"repo_status"}),
                allowed_capabilities=frozenset({"repo_read"}),
                max_risk_level_without_approval="read_only",
            ),
            invocation_store=store,
        )
        reg.register(
            ToolSpec(
                name="repo_status",
                description="status",
                params=[],
                handler=lambda: "clean",
                capabilities=frozenset({"repo_read"}),
                risk_level="read_only",
            )
        )

        result = await reg.invoke("repo_status", {})

        assert result.is_error is True
        assert store.records[0].status == "denied"
        assert "denied by policy" in (store.records[0].error or "")

    async def test_suggest_tier_records_approval_required_with_redacted_nested_values(
        self,
    ) -> None:
        store = ToolInvocationStore()
        reg = ToolRegistry(approval_tier="suggest", invocation_store=store)
        reg.register(_echo_spec())

        result = await reg.invoke(
            "echo",
            {
                "message": "hi",
                "headers": {"Authorization": "Bearer abc123"},
            },
        )

        assert result.is_error is True
        [record] = store.records
        assert record.status == "approval_required"
        assert record.redacted_arguments["headers"]["Authorization"] == "***"

    async def test_audit_store_write_failure_does_not_break_tool(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Issue #538: audit-store persistence failures should not break the execution path."""
        store = ToolInvocationStore()

        def _failing_append(*args: object, **kwargs: object) -> None:
            raise OSError("Disk full")

        monkeypatch.setattr(store, "append", _failing_append)

        reg = ToolRegistry(invocation_store=store)
        reg.register(_echo_spec())

        result = await reg.invoke("echo", {"message": "hi"})

        assert result.is_error is False
        assert result.content == "echo: hi"


class TestMcpToolDecorator:
    def test_decorator_attaches_spec(self) -> None:
        @mcp_tool(
            name="greet",
            description="say hi",
            params=[ToolParam(name="who", type="string", description="target")],
        )
        def greet(who: str) -> str:
            return f"hi {who}"

        spec = getattr(greet, "__mcp_tool__", None)
        assert isinstance(spec, ToolSpec)
        assert spec.name == "greet"
        assert spec.description == "say hi"
        assert spec.handler is greet

    def test_decorator_name_defaults_to_function_name(self) -> None:
        @mcp_tool(description="no name given", params=[])
        def auto() -> str:
            return "x"

        spec = auto.__mcp_tool__  # type: ignore[attr-defined]
        assert spec.name == "auto"

    def test_registry_register_from_function_accepts_decorated(self) -> None:
        @mcp_tool(description="d", params=[])
        def fn() -> str:
            return "x"

        reg = ToolRegistry()
        reg.register_from_function(fn)
        assert reg.get("fn").handler is fn

    def test_register_from_function_rejects_undecorated(self) -> None:
        def plain() -> str:
            return "x"

        reg = ToolRegistry()
        with pytest.raises(ToolRegistryError, match="not decorated"):
            reg.register_from_function(plain)


class TestUnclassifiedToolDenial:
    """Issue #537: unclassified tools should be denied under capability allowlists."""

    async def test_unclassified_tool_denied_under_readonly_allowlist(self) -> None:
        store = ToolInvocationStore()
        reg = ToolRegistry(
            policy=ToolPolicy.readonly_default(),
            invocation_store=store,
        )
        reg.register(
            ToolSpec(
                name="unclassified_tool",
                description="tool with no capabilities",
                params=[],
                handler=lambda: "result",
                capabilities=frozenset(),  # Empty capabilities
                risk_level="read_only",
            )
        )

        result = await reg.invoke("unclassified_tool", {})

        assert result.is_error is True
        assert "unclassified" in result.content
        assert "capability allowlist" in result.content
        [record] = store.records
        assert record.status == "denied"
        assert "unclassified" in (record.error or "")
