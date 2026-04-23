# gRPC status

Maxwell-Daemon does not currently ship a public gRPC service contract. The
package metadata exposes an optional `grpc` extra for future transport work, but
the repository does not include `.proto` definitions, generated stubs, or a
supported gRPC server entry point.

Use the [REST API](api.md) and [OpenAPI reference](openapi.md) for supported
remote control-plane integration today.

## Current contract

- No stable `.proto` files are published.
- No generated Python, TypeScript, or Go gRPC clients are published.
- No compatibility promise exists for a gRPC transport until proto definitions
  are committed and checked in CI.
- The REST/OpenAPI surface remains the source of truth for fleet control, task
  submission, gate status, artifacts, audit, and cost reporting.

## Generated-client Guidance

This section is roadmap-only guidance for the future gRPC transport. It explains
how generated clients should be produced once versioned proto files exist,
without implying that Maxwell-Daemon already supports gRPC.

When gRPC lands:

- keep versioned proto sources under a predictable path such as
  `proto/maxwell_daemon/v1/`;
- generate clients from the repository root, not from user-specific absolute
  paths;
- write generated output to repo-relative directories such as `gen/python/`,
  `gen/ts/`, or `gen/go/`;
- commit generator configuration files, not local shell history or machine
  paths;
- make CI regenerate stubs and fail on drift before a PR can merge.

The repository already exposes an optional `grpc` extra for contributor tooling:

```bash
pip install "maxwell-daemon[grpc]"
```

For Python clients, use `grpcio-tools` from the repo root once proto files
exist:

```bash
python -m grpc_tools.protoc \
  -I proto \
  --python_out=gen/python \
  --grpc_python_out=gen/python \
  proto/maxwell_daemon/v1/*.proto
```

For non-Python clients, prefer a checked-in generator config and repo-relative
output paths. A future TypeScript or Go flow can use a committed `buf.gen.yaml`
or equivalent generator config:

```bash
buf generate
```

Whatever generator is chosen, CI should run the same command in a clean checkout
and fail when regeneration changes tracked files:

```bash
git diff --exit-code
```

## Acceptance gate for adding gRPC

Before this page can become a true API reference, a gRPC implementation PR
should include:

- Versioned `.proto` files under a predictable path such as
  `proto/maxwell_daemon/v1/`.
- Generated-code guidance that does not require committing local machine paths.
- Server startup documentation covering TLS, auth, reflection, and port
  binding.
- Parity notes that map each gRPC service to the REST/OpenAPI route it mirrors
  or intentionally omits.
- Contract tests that regenerate stubs and fail when committed docs drift from
  the proto definitions.

Until then, treat gRPC as roadmap-only.
