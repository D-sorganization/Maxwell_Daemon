# Security

## Validation Sandbox Guarantees

The current validation sandbox is a policy gate around host subprocess execution. It is useful for repeatable local checks, evidence capture, and reducing accidental command exposure, but it is not an operating-system isolation boundary.

Current guarantees:

- Commands are executed as argv lists rather than through a shell.
- The executable name is checked against allow and deny lists.
- The working directory must resolve inside the configured workspace root.
- Environment variables are filtered to an explicit allowlist.
- Secret-looking environment values are redacted from summaries and artifacts.
- Audit-log payloads are recursively redacted for common secret keys such as
  `authorization`, `x-api-key`, `token`, and `password`, and bearer-looking
  strings are masked even when they appear under arbitrary nested keys.
- Commands have a timeout and recorded return code, duration, output summary, and policy evidence.

Current non-guarantees:

- No Docker or container runtime is started.
- No `--network none` policy is enforced.
- No filesystem namespace, read-only root filesystem, or host path isolation is enforced.
- No cgroup, seccomp, process, CPU, or memory limit is enforced.
- A permitted interpreter or build tool can still open files, use the network, spawn child processes, or run arbitrary project code with the user's host permissions.
- Audit redaction is heuristic rather than full DLP: values are masked by key
  name and bearer-token shape, not by generic entropy scanning.

## Operational Guidance

Run validation commands only for repositories and generated code you trust at the same level as any other local command. Treat the sandbox as a command policy, environment filter, and audit trail, not as protection against malicious code.

For higher-risk workloads, run Maxwell-Daemon inside an external boundary that you control, such as a disposable virtual machine, container, separate operating-system user, or locked-down CI runner. Keep secrets out of the process environment unless they are required for the specific task.

For multi-machine home fleets, pair these host-level sandbox cautions with the
[Tailscale fleet hardening guide](tailscale.md). Tailnet encryption is useful,
but daemon task, memory, artifact, and SSH APIs still need least-privilege
network policy plus Maxwell application auth.

Docker-backed isolation is tracked in [issue #468](https://github.com/D-sorganization/Maxwell-Daemon/issues/468). README and architecture claims must not describe Docker isolation, `--network none`, or perfect security until the runtime actually enforces those guarantees.

The broader security hardening backlog remains open in
[issue #490](https://github.com/D-sorganization/Maxwell-Daemon/issues/490) and
[issue #473](https://github.com/D-sorganization/Maxwell-Daemon/issues/473),
including failed-auth backoff, JWT refresh and revocation, tighter path/ID
validation, and shell-aware sandbox command restrictions.
