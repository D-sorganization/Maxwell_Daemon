"""Unit tests for maxwell_daemon.tools.mcp — the Model Context Protocol abstraction.

The abstraction exists so tool *definitions* live in one place (a ``ToolSpec``)
and *emit* provider-specific schemas (Anthropic, OpenAI) on demand. Handlers
run through the registry so every backend uses the same implementations.
"""

from __future__ import annotations

import pytest

from maxwell_daemon.tools.mcp import (
    ToolParam,
    ToolRegistry,
    ToolRegistryError,
    ToolResult,
    ToolSpec,
    mcp_tool,
)


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
                ToolParam(name="b", type="integer", description="optional", required=False),
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
                ToolParam(name="color", type="string", description="c", enum=["red", "blue"]),
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
        reg.register(ToolSpec(name="zulu", description="z", params=[], handler=lambda: "z"))
        reg.register(ToolSpec(name="alpha", description="a", params=[], handler=lambda: "a"))
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

        spec = auto.__mcp_tool__
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
