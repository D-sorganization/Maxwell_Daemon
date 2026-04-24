# Tool Authoring and MCP Boundaries

This guide is for contributors adding deterministic tools that agents can call
inside Maxwell-Daemon. Tools are not mini-orchestrators. A tool should expose one
bounded capability, return structured evidence, and let Maxwell's policy,
approval, audit, and gauntlet layers decide whether the result can advance.

## Current Tool Runtime

Maxwell's local tool surface is built around these contracts:

| Contract | Purpose |
| --- | --- |
| `ToolSpec` | Declares the tool name, description, parameters, handler, capabilities, risk level, and approval requirement. |
| `ToolParam` | Defines JSON-schema-compatible input fields for model-facing validation. |
| `mcp_tool` | Decorates a Python callable with a `ToolSpec` so it can be registered without duplicating metadata. |
| `ToolRegistry` | Holds tool specs, emits OpenAI and Anthropic tool schemas, and invokes handlers. |
| `ToolPolicy` | Blocks tool ids, capabilities, risk levels, or approval-required tools before handlers run. |
| `ToolInvocationStore` | Records redacted invocation attempts for later audit and critic review. |
| `HookRunnerProtocol` | Allows `pre_tool` and `post_tool` hooks to block or fail tool use without relying on the model to comply. |

The built-in tools live in `maxwell_daemon.tools.builtins`. They bind every
filesystem and shell operation to a workspace root, reject path traversal, redact
sensitive arguments, truncate command output, and require approval for write,
network, or command-execution capabilities.

## MCP Status Boundary

The module name `maxwell_daemon.tools.mcp` is a local compatibility layer for
model-facing tool schemas. It is not a public Model Context Protocol server or
client transport today.

Current behavior:

- `ToolRegistry.to_openai()` emits OpenAI function-calling schema dictionaries.
- `ToolRegistry.to_anthropic()` emits Anthropic tool schema dictionaries.
- `ToolRegistry.invoke()` dispatches local Python handlers with policy, hooks,
  approval-tier checks, and optional invocation audit.
- Built-in tools run inside Maxwell's process and workspace policy.

Not currently shipped:

- an MCP server entry point;
- MCP client transport handling;
- protocol-level session negotiation;
- remote resource or prompt exposure through MCP;
- compatibility tests against third-party MCP clients.

When full MCP support is added, keep this local registry as the in-process
contract and add a separate transport adapter around it. The transport adapter
should translate protocol requests into `ToolRegistry` calls; it should not
duplicate policy, approval, redaction, or invocation logging.

## Authoring Checklist

Use this checklist before adding a new tool:

1. Define the smallest operation that another agent or gate needs.
2. Choose capabilities from `ToolCapability`.
3. Choose the lowest honest `ToolRiskLevel`.
4. Set `requires_approval=True` for local writes, command execution, network
   writes, external side effects, or destructive operations.
5. Declare every model-facing argument as a `ToolParam`.
6. Validate nested structures inside the handler when simple schema types are
   not enough.
7. Return structured text or machine-readable identifiers that downstream gates
   can inspect.
8. Redact secrets from arguments, stdout, stderr, command lines, URLs, headers,
   and exception details.
9. Add focused tests for schema emission, successful invocation, policy denial,
   approval behavior, and redaction.

Do not hide workflow policy inside a tool. A tool may read data, generate an
artifact, run a bounded command, or propose a write. It should not merge pull
requests, waive gates, bypass critic review, or silently spend a paid provider
budget.

## Minimal Read-Only Tool

```python
from maxwell_daemon.tools.mcp import ToolParam, ToolRegistry, mcp_tool


@mcp_tool(
    name="count_lines",
    description="Count newline-separated lines in provided text.",
    capabilities=frozenset({"repo_read"}),
    risk_level="read_only",
    params=[
        ToolParam(
            name="text",
            type="string",
            description="Text to count",
        )
    ],
)
def count_lines(text: str) -> str:
    return str(len(text.splitlines()))


registry = ToolRegistry()
registry.register_from_function(count_lines)
```

Prefer `register_from_function()` for decorator-backed tools and
`register(ToolSpec(...))` when tests need a deliberately small inline spec.

## Workspace-Bound Tools

Use the built-in factory pattern for tools that touch the repository:

```python
from pathlib import Path

from maxwell_daemon.tools.builtins import make_read_file
from maxwell_daemon.tools.mcp import ToolRegistry, ToolPolicy


root = Path("/work/my-repo")
registry = ToolRegistry(policy=ToolPolicy.readonly_default())
registry.register_from_function(make_read_file(root))
```

Rules for workspace tools:

- resolve all paths relative to the assigned workspace root;
- reject absolute paths or symlinks that escape the root;
- inject runners or services so tests do not spawn real subprocesses;
- cap runtime and output size;
- return artifact ids for large outputs instead of dumping full logs into model
  context.

## Local Test Harness

Add tests before wiring a tool into agent loops. Keep unit tests deterministic
and local:

```bash
python -m pytest tests/unit/test_tools_mcp.py -q
python -m pytest tests/unit/test_tools_builtins.py -q
python -m pytest tests/unit/test_tools_hooks_integration.py -q
python -m ruff check maxwell_daemon/tools tests/unit/test_tools_mcp.py
```

Useful harness patterns:

- use `tmp_path` for workspace roots;
- use inline `ToolSpec` objects for registry edge cases;
- use injected `BashRunner` callables instead of real shells;
- use fake hook runners to assert `pre_tool` and `post_tool` behavior;
- attach `ToolInvocationStore` to a temporary JSONL path when testing audit
  records;
- assert `ToolPolicy.readonly_default()` blocks write, shell, network, and
  approval-required tools;
- assert token-like argument values are redacted in stored invocation records.

If the tool depends on network access, browser automation, GitHub, or a paid
provider, keep that dependency behind an injected service and cover the adapter
with mocks. Real external calls belong in an explicit integration test that is
skipped unless the required credentials and local binaries are present.

## Review Gates for New Tools

A tool-authoring PR should not pass review until it provides:

- contract tests for schema emission and handler invocation;
- policy tests for denial and approval-required paths;
- redaction tests for arguments and captured output;
- workspace-boundary tests for any filesystem access;
- timeout or cancellation coverage for commands, browser actions, or network
  calls;
- docs for any new user-visible command, config key, or risk boundary.

The critic panel should reject tools that smuggle orchestration decisions into
handlers, run without workspace isolation, expose unredacted secrets, or require
live provider credentials for unit tests.
