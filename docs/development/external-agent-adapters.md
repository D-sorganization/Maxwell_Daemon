# External Agent Adapter Template

Maxwell external-agent adapters are the boundary between orchestration policy and
market coding tools such as Codex CLI, Claude Code, Continue, Aider, Cline,
OpenHands, Jules, and local model runners. The adapter owns provider-specific
invocation details only. Scheduling, gate policy, repository ownership, merge
decisions, and approval rules stay outside the adapter.

## Contract

New adapters implement `ExternalAgentAdapterProtocol` from
`maxwell_daemon.backends.external_adapter`.

Required members:

- `adapter_id`: stable, non-empty identifier used in configuration and evidence.
- `capabilities`: `ExternalAgentCapability` describing operations, roles,
  context limits, credentials, binaries, workspace needs, cost/quota hints, and
  safety notes.
- `probe(spec)`: returns sanitized availability and version diagnostics.
- `run(context)`: executes one declared operation and returns structured evidence.
- `cancel(context)`: records a best-effort cancellation request.

Required operations are modeled as `ExternalAgentOperation`: `probe`, `plan`,
`implement`, `review`, `validate`, `checkpoint`, and `cancel`. An adapter may
declare only the subset it safely supports. Legacy `read` and `write` remain for
compatibility with the first contract slice.

## Capability Checklist

Populate every field that is knowable without executing work:

- Stable adapter id and display name.
- Probe/version source, such as `tool --version`.
- Supported roles and operations.
- Tags that aid routing, such as `cli`, `local`, `non-interactive`, or
  `review-only`.
- Context limits and cost/quota model.
- Required credentials and local binaries.
- Workspace requirements.
- Whether the adapter can edit files, run tests, or execute in the background.
- Terms, safety, and policy notes.

## Run Evidence

Every run result should fill the fields Maxwell gates and UI need:

- `status`, `summary`, and optional `details`.
- `changed_files` for write-capable runs.
- `commands_run` and `tests_run`.
- `artifacts` for reports, patches, logs, or machine-readable findings.
- Cost/quota estimates when available.
- Redacted stdout/stderr snippets.
- `checkpoint` text that can restart work after interruption.
- `policy_warnings` for terms, safety, or gate concerns.

Do not return raw secrets. `ExternalAgentRunResult.completed()`,
`unavailable()`, and `cancelled()` redact common token, API key, password, and
authorization header forms before returning.

## Safety Rules

- Write-capable operations must require a workspace assignment.
- Review, plan, validate, checkpoint, probe, and read operations must not report
  file mutations.
- Adapters must not merge PRs directly.
- Provider-specific command construction stays in the adapter.
- Gate decisions and policy approvals stay in Maxwell's gate layer.

## Minimal Adapter Skeleton

```python
from maxwell_daemon.backends.external_adapter import (
    ExternalAgentAdapterBase,
    ExternalAgentCapability,
    ExternalAgentOperation,
    ExternalAgentProbeResult,
    ExternalAgentProbeSpec,
    ExternalAgentRunContext,
    ExternalAgentRunResult,
)


class ExampleCLIAdapter(ExternalAgentAdapterBase):
    adapter_id = "example-cli"
    capabilities = ExternalAgentCapability(
        adapter_id=adapter_id,
        display_name="Example CLI",
        supported_roles=frozenset({"planner", "reviewer"}),
        supported_operations=frozenset(
            {
                ExternalAgentOperation.PROBE,
                ExternalAgentOperation.PLAN,
                ExternalAgentOperation.REVIEW,
                ExternalAgentOperation.CHECKPOINT,
                ExternalAgentOperation.CANCEL,
            }
        ),
        required_binaries=("example",),
        can_edit_files=False,
        supports_background=True,
        safety_notes=("read-only wrapper; merge decisions stay outside the adapter",),
    )

    def _probe(self, spec: ExternalAgentProbeSpec) -> ExternalAgentProbeResult:
        return ExternalAgentProbeResult(
            adapter_id=self.adapter_id,
            summary="example CLI available",
            version="1.2.3",
            details=("example --version",),
        )

    def _run(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult:
        return ExternalAgentRunResult.completed(
            adapter_id=self.adapter_id,
            operation=context.operation,
            summary="result summary",
            commands_run=("example run --json",),
            stdout_snippet="{...}",
            checkpoint="state needed to resume",
        )
```

Use `CodexCLIExternalAgentAdapter` as the reference wrapper for an existing
Maxwell backend. It exposes the existing `CodexCLIBackend` in safe suggest mode
without changing backend behavior or granting write access.
