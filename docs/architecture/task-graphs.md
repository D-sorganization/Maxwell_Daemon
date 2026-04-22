# Task Graphs

Task graphs are Maxwell's typed handoff model for named sub-agent delivery.
They describe which role should run, which artifacts that role needs, and which
artifact it must produce before downstream roles can continue.

The first implementation is intentionally a service-layer foundation:

- `TaskGraph` validates a directed acyclic graph of `GraphNode` definitions.
- `GraphNode` names an `AgentRole`, dependency node IDs, required artifact
  kinds, backend/model overrides, retry policy, and output artifact kind.
- Built-in templates cover `micro-delivery`, `standard-delivery`, and
  `security-sensitive-delivery`.
- `GraphRunner` executes nodes sequentially in dependency order and stores
  each node's output through the durable artifact store.

## Built-In Templates

`micro-delivery` uses one implementer/QA node for low-risk work items with no
more than two acceptance criteria.

`standard-delivery` runs:

```text
planner -> implementer -> qa -> reviewer
```

`security-sensitive-delivery` inserts a security role before final review:

```text
planner -> implementer -> qa -> security -> reviewer
```

Template selection is deterministic. High or critical risk, or a security
label, selects the security-sensitive graph. Low-risk small work selects the
micro graph. Everything else selects the standard graph.

## Handoff Artifacts

Nodes do not rely on hidden conversation state. A node declares the artifact
kinds it requires, and the runner passes dependency artifact text into that
node's execution context. A completed node must produce at least one artifact;
empty output is retried according to the node policy and blocks the graph after
retry exhaustion.

This keeps graph execution compatible with future parallel scheduling. The
current runner is sequential, but the model already separates graph validation,
node readiness, artifact dependencies, and node execution.
