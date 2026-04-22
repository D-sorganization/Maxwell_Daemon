# Maxwell Eval Harness

The `maxwell-daemon eval` command runs deterministic workflow scenarios without
network access, model provider keys, or GitHub writes. The built-in smoke suite
uses fixture references, a scripted fake agent, required-check metadata, and
scoring profiles to produce stable regression reports.

Current starter scenarios cover:

- a single-file bug fix with regression-test evidence,
- a GAAI story evidence import workflow,
- an approval-required tool-policy workflow.

Typical local commands:

```powershell
maxwell-daemon eval list
maxwell-daemon eval run --output .maxwell/evals
maxwell-daemon eval report <run-id> --output .maxwell/evals
maxwell-daemon eval compare <base-run-id> <candidate-run-id> --output .maxwell/evals
```

Normal CI smoke evals must remain deterministic. Live model or provider-backed
evaluations should be added behind explicit opt-in flags and should not replace
the fixture suite.
