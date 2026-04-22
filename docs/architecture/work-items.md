# Governed work items

Work items are Maxwell's durable authorization contract for autonomous delivery.
They capture human intent, executable scope, acceptance criteria, required checks,
and delivery state before tasks or agents mutate a repository.

`Task` remains the execution unit. A task answers "what is running now?" A work
item answers "what has been authorized, under what constraints, and how will it
be verified?"

## Lifecycle

Work items move through explicit transitions:

- `draft -> needs_refinement -> refined`
- `refined -> in_progress`
- `in_progress -> done | blocked | cancelled`
- `blocked -> needs_refinement | refined | cancelled`

Invalid transitions are rejected by the model/store boundary. A `refined` work
item must have at least one acceptance criterion. An `in_progress` item records
`started_at`, and a `done` item records `completed_at`.

## Minimal JSON

```json
{
  "title": "Add source-controlled checks",
  "repo": "D-sorganization/Maxwell-Daemon",
  "acceptance_criteria": [
    {
      "id": "AC1",
      "text": "Checks load from .maxwell/checks/*.md",
      "verification": "pytest tests/unit/test_checks_loader.py"
    }
  ],
  "required_checks": ["pytest", "ruff"],
  "priority": 10
}
```

The REST API exposes `/api/v1/work-items`; the CLI exposes
`maxwell-daemon work-item`.
