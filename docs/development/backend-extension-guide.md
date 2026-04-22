# Backend and Extension Development Guide

This guide is for contributors adding a new LLM backend, CLI-backed agent, tool
surface, or other Maxwell extension. Keep the extension boundary small: the
extension owns provider-specific behavior, while orchestration policy, task
ownership, budget checks, approval rules, and merge decisions stay in Maxwell's
core services.

## Choose the Right Extension Point

Use the narrowest interface that matches the behavior:

| Goal | Extension point | Primary contract |
| --- | --- | --- |
| Add a chat/completion provider | `maxwell_daemon.backends.ILLMBackend` | `BackendResponse`, `TokenUsage`, `BackendCapabilities` |
| Wrap an external coding CLI | `ExternalAgentAdapterProtocol` | `ExternalAgentCapability`, probe/run/cancel results |
| Add a model/tool callable by agents | `ToolRegistry` registration | JSON-schema-compatible parameters and structured results |
| Add source-controlled quality gates | `.maxwell/checks/*.toml` | check trigger, command, timeout, and result metadata |
| Add durable workflow state | task store or action ledger APIs | explicit identifiers, timestamps, and audit evidence |

If an extension needs more than one surface, implement the smallest independent
slice first and add integration after the contract is covered by tests.

## Backend Implementation Checklist

1. Create a provider module under `maxwell_daemon/backends/`.
2. Implement `ILLMBackend` from `maxwell_daemon.backends.base`.
3. Return `BackendResponse` with complete usage and finish metadata.
4. Report `BackendCapabilities` honestly; routing and budget code rely on it.
5. Register the backend in `maxwell_daemon/backends/registry.py`.
6. Add configuration fields only when the existing model cannot express the
   provider requirements.
7. Add unit tests with mocked provider I/O. Unit tests must not call real APIs.
8. Add docs for any user-facing configuration, model naming, or limitations.

Prefer shared helpers over provider-specific duplication for common behavior
such as cost calculation, retry policy, response normalization, and token usage
mapping. Keep provider SDK calls inside the backend module so callers depend on
the Maxwell contract, not on the vendor client.

## External Agent Adapter Checklist

Use an external agent adapter when the provider is a coding tool rather than a
plain completion API.

1. Implement `ExternalAgentAdapterProtocol` or subclass
   `ExternalAgentAdapterBase`.
2. Declare a stable `adapter_id` and complete `ExternalAgentCapability`.
3. Implement `probe()` so operators can diagnose missing binaries or credentials
   without starting work.
4. Implement `run()` for only the operations the adapter can safely support.
5. Return `ExternalAgentRunResult` with changed files, commands, tests,
   artifacts, cost notes, policy warnings, and a resume checkpoint when known.
6. Redact secrets from stdout, stderr, command lines, and details.
7. Require an assigned workspace for any write-capable operation.
8. Keep merge, approval, and gate decisions outside the adapter.

Local plugin descriptors can register adapters without adding them to the core
package. Use that path for experimental integrations or environment-specific
wrappers.

## Tool Extension Checklist

Tools are the safest place to expose small deterministic capabilities to agent
loops.

1. Define a focused function with typed inputs and explicit failure behavior.
2. Register it through the existing tool registry path.
3. Use schema-compatible parameter types so callers can validate before
   execution.
4. Return structured data instead of prose when another tool or gate will read
   the result.
5. Keep filesystem, network, and process execution permissions explicit.
6. Add tests for validation failures, successful output, and redaction.

Do not hide orchestration policy inside a tool. If the tool starts background
work, mutates a repository, or spends budget, it should emit durable evidence
that higher-level gates can inspect.

## Testing Expectations

Follow test-driven changes for extension work:

- Start with contract tests for validation and failure modes.
- Mock provider APIs, subprocesses, and filesystem boundaries.
- Add one integration-style test only when multiple Maxwell surfaces must work
  together.
- Cover secret redaction for anything that captures stdout, stderr, headers, or
  environment-derived configuration.
- Run targeted tests first, then the relevant lint/type checks for changed
  modules.

Useful commands:

```bash
python -m pytest tests/unit/test_backends.py -q
python -m pytest tests/unit/test_tools_builtins.py -q
python -m ruff check maxwell_daemon tests
python -m mypy maxwell_daemon
python -m mkdocs build --strict
```

Adjust the test file names to the extension surface you touched.

## Design Boundaries

Use these boundaries to keep extensions maintainable:

- DbC: validate required fields at the contract boundary and return explicit
  errors instead of allowing provider-specific exceptions to leak outward.
- LOD: callers should talk to Maxwell interfaces, not nested SDK objects or
  subprocess details.
- DRY: share normalization, redaction, pricing, and capability helpers when two
  providers need the same rule.
- Auditability: persisted task, artifact, and action records should identify
  the extension, operation, workspace, commands, and validation evidence.
- Reversibility: an extension should be removable without breaking unrelated
  backends, tools, or docs.

When in doubt, add the smallest contract-first slice and leave richer routing,
UI, and policy work to follow-up issues.
