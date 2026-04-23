# Gate Runtime and Critic Panel

Maxwell-Daemon treats gates as the boundary between delegated work and user-visible
progress. A delegate can plan, edit, test, or review, but the gate runtime decides
whether that work is allowed to move to the next phase.

The current implementation has three layers:

| Layer | Module | Responsibility |
| --- | --- | --- |
| Gate execution | `maxwell_daemon.core.gates` | Runs ordered gates through narrow adapters and applies required, optional, waiver, and continue-on-failure rules. |
| Gauntlet state | `maxwell_daemon.core.gauntlets` | Models gate runs, decisions, evidence, waivers, transitions, and final gauntlet status. |
| Critic panel | `maxwell_daemon.core.critics` | Runs adversarial critics, aggregates findings, and exposes the panel as a gate adapter. |

This keeps orchestration policy separate from tool-specific execution. Gate
adapters see one gate definition at a time. Stores preserve the evidence and
transition history. The runtime owns ordering and fail-closed behavior.

## Gauntlet Lifecycle

1. A caller defines an ordered gate list for a work item, pull request, release,
   or other target.
2. `GauntletRuntime` validates that gate ids are unique and each gate has a
   registered adapter.
3. Gates run in declared order.
4. A required failed gate stops later gates unless `continue_on_failure` is set.
5. Optional failed gates are recorded without blocking completion.
6. Waived gates keep the original failed result visible and record a separate
   waiver decision.
7. The final decision summarizes executed gates, skipped gates, failed gates,
   waived gates, and evidence pointers.

The gauntlet state model is stricter than the simple execution summary. It
tracks gate run status transitions from `pending` to `running` to terminal
states such as `passed`, `failed`, `waived`, `blocked`, or `error`. Completed
gates require a decision and completion timestamp. Failed gates require evidence
and at least one reason.

## Gate Decisions

Gate decisions are intentionally structured:

- `pass` means the gate accepted the evidence.
- `fail` means the gate found a blocker or could not validate a required claim.
- `needs_human` means the gate cannot decide without explicit user input.
- `waived` means a human accepted a known failed gate.

A gauntlet cannot be marked passed while an unwaived required gate remains
failed. This rule is enforced by the model layer so callers cannot accidentally
turn a failed validation into a successful run.

## Critic Panel Gate

The critic panel is Maxwell's adversarial review layer. It is designed for
independent critics such as architecture, tests, security, maintainability,
product fit, and release readiness.

Critic profiles define:

- stable critic id and display name;
- adapter id;
- whether the critic is required;
- timeout policy;
- metadata used by routing or prompt construction.

Critic adapters return `CriticPanelRun` records with structured findings.
Findings preserve critic id, severity, summary, optional file and line, and
evidence strings. `p1` findings are blocking by default. `p2` findings are
visible but non-blocking.

`CriticAggregatePolicy` sorts runs and findings deterministically before
building a verdict. Required missing critics, required timeouts, and required
adapter errors fail closed by producing blocking findings. Optional execution
issues can be recorded as non-blocking when policy allows.

The panel can be bridged into the gate runtime through
`CriticPanelRunner.as_gate_adapter(...)`. This lets a critic panel participate
in the same gauntlet as tests, CI, budget, sandbox, human approval, or release
readiness gates.

## Waivers

Waivers are exceptions, not rewrites.

`GateWaiver` and `WaiverRecord` require an actor and reason. A waived gate keeps
the original failed verdict and evidence visible, then records the waiver as an
additional decision. The final gauntlet status becomes `passed_with_waivers`
when all required failures were waived.

Use waivers for explicit user-approved risk, not for flaky automation. If a
gate is unreliable, fix the gate or mark it optional until it is trustworthy.

## Adapter Boundaries

Gate and critic adapters should be small and read-only unless a future policy
explicitly grants remediation authority.

Adapters should:

- return structured evidence instead of prose-only summaries;
- avoid direct merge, deployment, or destructive APIs;
- redact secrets from command output and logs;
- report missing tools, timeouts, and parse errors as gate-visible failures;
- leave scheduling, retries, waivers, and final decisions to the runtime.

This boundary lets Maxwell use many market tools without giving each tool its
own incompatible definition of "done".

## Current Implementation Boundaries

The core runtime, gauntlet models, in-memory stores, critic aggregation, and
critic-to-gate bridge are implemented and covered by focused unit tests.

Follow-up integration work should add:

- durable store wiring for production gauntlet history;
- CLI commands for listing, running, inspecting, and waiving gates;
- REST endpoints for work-item gauntlets and gate waivers;
- adapters for source-controlled checks, CI status, sandbox validation, budget
  policy, and human approval;
- dashboard views for live gauntlets and critic findings.

Until those integrations exist, callers should treat the core modules as the
contract-first foundation rather than a complete end-user gate console.
