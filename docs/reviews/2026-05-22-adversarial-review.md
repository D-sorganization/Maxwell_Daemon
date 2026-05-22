# Maxwell-Daemon — Comprehensive Adversarial Review (A–O)

**Date:** 2026-05-22
**Scope:** Full codebase (47,782 LOC Python · 219 files · 31 subpackages), UI, ops surface, security posture, competitive position.
**Method:** Code archaeology (god-objects, exception sites, type errors), static-analysis triangulation (bandit, mypy, ruff outputs in repo), security path tracing, competitor scan.
**Verdict:** **Strong engineering foundation, not yet production-grade.** Composite score **B− (3.0/4.0)**.

This review supersedes the 2026-04-30 `ADVERSARIAL_REVIEW_SUMMARY.md` and the 2026-04-19 `A-N_Assessment`. Issues #793–#803 are closed but several findings recur — the rubric below tracks *current* state, not historical state.

---

## 0. Executive Summary

Maxwell-Daemon is the **AI control plane** in a three-repo fleet: it orchestrates a Strategist / Implementer / Crucible state machine across 25+ LLM backends, exposes a stable HTTP+WebSocket contract, enforces gauntlets (policy gates) and sandbox execution, and ships a cost ledger and SHA-256 audit chain. Test discipline is real (91% coverage, 205 test files). Documentation density is real (13 doc trees, mkdocs site).

The blockers are familiar to anyone shipping infrastructure software:

| Blocker | Why it bites | Fix size |
|---|---|---|
| `api/server.py` is **3,182 lines / 111 endpoints** | Cross-concern coupling, slow CI, scary refactors | 2–3 weeks |
| `daemon/runner.py` is **1,906 lines / 75 methods** with hard `sqlite3` coupling | Untestable state machine; horizontal scale blocked | 3–4 weeks |
| **128 `except Exception`** sites (95 grandfathered `# noqa: BLE001`) — including the audit-write path | Silent failures, lossy errors at the customer boundary | 2 weeks |
| **Single-process in-memory rate limiter** with `allow_credentials=True` CORS path | DoS bypass + credential leak under multi-instance deploy | 1–2 weeks |
| **`hooks.py` runs config-supplied commands via `create_subprocess_shell`** | RCE if config write-path is ever compromised | 2 days |
| **Sandbox = host subprocesses** (no Docker / VM / namespace isolation) | Cannot safely run untrusted generated code | 4–6 weeks |
| **Vanilla-JS `/ui/` is a 1,917-line single file** | UX is the part of the product that sells; this loses to Cursor on sight | 6–8 weeks |

A focused 12–14 week effort (described in §17 *Production Epic*) closes all of these. Nothing here is a research problem — every fix has a known shape.

---

## 1. The A–O Rubric — Grades With Evidence

Grades are A (production-grade) → F (broken). Plus / minus reflect velocity of fix.

| # | Dimension | Grade | One-line verdict |
|---|---|---|---|
| **A** | Architecture & module decomposition | **C+** | 31 packages cleanly named; two god-objects swallow the design |
| **B** | Code quality & complexity | **C** | Ruff/mypy configured strict; ~94 `noqa: BLE001` + grandfathered `C901` carve-outs hide debt |
| **C** | Exception handling & error surface | **D+** | 128 `except Exception` sites; HTTP layer collapses typed errors to generic 409/500 |
| **D** | Type safety & static analysis | **C−** | mypy strict declared; 20+ untyped-decorator errors in `mcp/`; `ignore_missing_imports` on `anthropic`/`openai` |
| **E** | Test coverage & strategy | **B+** | 91% line coverage; 195 unit tests but only **5 integration tests** and no load suite |
| **F** | Security: authn/authz | **A−** | JWT HMAC, RBAC, GitHub App rotation, header redaction — clean |
| **G** | Input validation & sandbox | **C** | Pydantic-validated boundary, but sandbox is host subprocess; `hooks.py:351` is `create_subprocess_shell` |
| **H** | Rate limiting / DoS / backpressure | **C** | Token bucket + sliding window — in-memory only; multi-instance ⇒ 10× bypass |
| **I** | Observability | **B+** | Prometheus, structlog, correlation IDs, Grafana dashboard shipped; no distributed tracing; no SLO doc |
| **J** | Operational readiness | **B** | Multi-stage Docker, hardened systemd, backup with hashes; missing runbook, no audit forwarding |
| **K** | Scalability | **D+** | SQLite + in-process state ⇒ single-instance ceiling; no PG path, no Redis path, no leader election |
| **L** | API contract & docs | **B** | OpenAPI auto-served, append-only contract, `/api/version` advertised; deprecation policy undocumented |
| **M** | UI/UX & accessibility | **C** | Lightweight vanilla JS; 1,917-line monolithic `app.js`; no ARIA pass, no real dark mode toggle |
| **N** | Feature completeness vs competitors | **A−** | 25+ backends, Crucible loop, gauntlets, cost ledger, task graphs — beats Aider/Continue on orchestration |
| **O** | Developer experience & extensibility | **A−** | 14+ CLI commands, MCP registry, 5 IDE extensions + Electron desktop app |

**Composite (weighted by ship-impact):** **B− / 3.0** — *good engineering, not yet trustworthy infra.*

---

## 2. (A) Architecture & Module Decomposition — **C+**

**Map.** `maxwell_daemon/` is 31 well-named subpackages: `api/ backends/ cli/ core/ daemon/ director/ executor/ fleet/ gh/ memory/ model_routing/ sandbox/ session/ ssh/ tools/ …`. Boundary discipline at the package level is real.

**Where it falls apart.**

- `api/server.py` — **3,182 lines / 111 functions / 38 classes**. Mounts every route, auth dep, SSH endpoint, fleet endpoint, eval endpoint, webhook handler. This is the file the prior epic (#793) targeted. Routes are *partially* extracted to `api/routes/{status,health,tasks,control_plane,…}.py`, but the server itself didn't shrink enough.
- `daemon/runner.py` — **1,906 lines / 75 methods**. `Daemon.__init__` instantiates 18+ stores/services directly. Twenty-plus internal imports. `DaemonState` re-exported with `# noqa: E402` at line 71 (a circular-import band-aid).
- `core/delegate_lifecycle.py` — **1,215 lines** with hard `import sqlite3` at the top. There is no `PersistenceBackend` ABC; the state machine and storage are fused.

**Coupling smell.** `daemon/runner.py` imports `fleet/`, `director/`, `events`, `core/*`, `backends/*`, `memory/*` — everything imports up into the runner. The fleet coordinator should *consume* the daemon, not live inside it.

**Why it matters.** A 3,182-line file means: (1) IDE jump-to-definition becomes slow, (2) PR review fatigue is the silent killer of code review quality, (3) every test in `tests/unit/test_api.py` shares a fixture surface, so an edit to /v2/status can flake an SSH test, (4) you cannot extract a service or refactor a router without taking the whole file hostage.

**What good looks like.** No file > 600 lines. Routes live in `api/routes/*.py`. Daemon is composed via DI: `Daemon(store, ledger, bus, scheduler, fleet)`. `core/persistence.py` defines `TaskStore`, `LedgerStore`, `DelegateStore` as Protocols, with `sqlite/` and `postgres/` implementations.

---

## 3. (B) Code Quality & Complexity — **C**

`pyproject.toml` selects ruff `E,F,W,I,N,UP,B,C4,SIM,RUF,C90,BLE` — a *good* selection. Then it grandfathers the two rules that hurt the most:

```toml
# BLE: ~94 sites grandfathered with inline `# noqa: BLE001` (#795)
# C901: max-complexity = 15  (industry standard is 10–15)
#       with inline `# noqa: C901` carve-outs (#794, #793, #798)
```

That's the right *direction* but it is a **debt ledger, not a fix**. New code must pass; the legacy lava field is still there, including the two largest functions in the daemon. Mypy is in strict mode but `[mypy.overrides]` ignores `anthropic`, `openai`, `mcp` — i.e., the typed-decorator surface of every adapter is unchecked.

**Action.** Burn-down the noqa list at ~10 sites/week, gated by a CI ratchet that disallows *adding* new `# noqa: BLE001` or `# noqa: C901` lines. Make the inventory file (`suppressions_nosec.txt` is the pattern) a release-blocker for v1.0.

---

## 4. (C) Exception Handling & Error Surface — **D+**

**The smoking gun:** `api/server.py:1588` catches `Exception` and maps it to `HTTPException(409, str(exc))`. That handler sits over `submit_task()`. `DuplicateTaskIdError`, `BudgetExceededError`, `BackendUnavailableError`, `RateLimitExceededError`, `ValidationError` — all collapse to the same 409 with a stringified message body. The dashboard cannot retry intelligently, the operator cannot alert on category, and a customer integration cannot do anything but log-and-pray.

**The dangerous one:** `audit.py:375` catches `Exception` inside the audit-write path and logs (recursively, into the same logging layer that may itself be the failure source). If the audit chain ever *truly* fails to extend, that failure is invisible. For a compliance feature, that is unsafe.

**The pattern:** 128 sites, 95 of them `# noqa: BLE001`. Most are "swallow and continue" — defensible at the daemon-loop level (you don't want one task killing the runner) but inappropriate at HTTP boundaries and persistence boundaries.

**Action.**
1. Define a typed exception tree: `MaxwellError → {ClientError, BackendError, PolicyError, StorageError, BudgetError}`.
2. Single FastAPI `exception_handler` translates the tree to RFC 7807 `problem+json` with stable `type` URIs.
3. Audit path catches `OSError` only; anything else propagates and crashes the writer with a poison-pill record so loss is visible.

---

## 5. (D) Type Safety & Static Analysis — **C−**

Mypy strict is the right posture. The output in `mypy_output.txt` shows the cracks:

- 20+ "untyped decorator" errors in `mcp/server/__init__.py` — chained `@server.tool(...)` decorators from the MCP SDK lack stubs, silently producing `Any`-typed handlers.
- Several `Unused "type: ignore[untyped-decorator]"` warnings — meaning past suppressions are now wrong.
- `tools.uv.lock` shows `types-pyyaml` is the *only* stub package; missing stubs for `anthropic`, `openai`, `httpx`, `keyring`, `prometheus-client`.

`tests/` `mypy_test_errors.txt` is 10 KB of errors — tests aren't type-checked in CI.

**Action.** Add `types-*` stubs to dev deps; ship a `_typing.py` shim with `Protocol`s for MCP decorator targets; type-check tests in CI (separate strictness profile is fine).

---

## 6. (E) Test Coverage & Strategy — **B+**

195 unit tests, **5 integration tests**, BDD harness present, benchmark suite gated behind explicit `-m benchmark`. Coverage gate is 34% (declared) — actual is **91%**. Three observations:

1. The 34% gate is *dangerous*: it means coverage can regress by 57 points before CI complains. Lift to 80% immediately.
2. The 195:5 unit:integration ratio is the inverted pyramid. Real bugs in this system live in the *seams*: daemon ↔ fleet, sandbox ↔ executor, websocket ↔ scheduler. There are essentially no tests there.
3. There is no load test. The README claims "10–20 tasks/second"; nothing in CI proves it.

**Action.**
- Bring integration coverage to ≥ 25 files (daemon-lifecycle, fleet-failover, websocket-fanout, sandbox-escape, audit-rotate, cost-ledger).
- Add `tests/load/` with `locust` or `vegeta` driving `/api/dispatch` and `/api/v1/events` WS; gate at 100 RPS for a 10-min window on every release.
- Raise `--cov-fail-under` to 80, then 85 by v1.0.

---

## 7. (F) Authentication & Authorization — **A−**

Genuinely strong:

- `auth.py`: JWT HMAC, algorithm whitelist (HS256 only — algorithm-confusion fixed), required-claims enforcement, leeway window, role enum.
- `github_auth.py`: GitHub App installation tokens, 1-hour TTL with auto-refresh.
- RBAC dep factory `_make_rbac_dep` at `api/server.py:694` is reusable across routes.
- Audit log redacts `Authorization`, `X-API-Token`, `Cookie` before chaining.

**Two paper cuts.**
- Static API tokens (`X-API-Token`) have no rotation tooling. Add a `tokens rotate` CLI subcommand and a TTL field on the token model.
- JWTs are still served to the browser as bearer tokens (not HTTPOnly cookies). The 2026-04 review flagged this (#797); the code path is still there. XSS in `/ui/` ⇒ token theft.

---

## 8. (G) Input Validation & Sandbox Isolation — **C**

Two layers, very uneven.

**HTTP boundary — strong.** `api/validation.py` defines `PromptField`, `RepoField`, `TaskIdField`, `RoutingKeyField` with regex + length constraints; `api/contract.py` is versioned; Pydantic v2 throughout.

**Execution boundary — weak.** `sandbox/policy.py` enforces argv-allowlist + cwd containment + env filter + timeout + output redaction. *Good* against accidental damage. *Useless* against motivated escape: it's a host subprocess, in the same UID, with the same filesystem. The README is honest about this; the consequence is that **you cannot safely run untrusted generated code**. That ceiling matters when Maxwell's selling point is "autonomous."

**Hooks — dangerous.** `maxwell_daemon/hooks.py:351` uses `asyncio.create_subprocess_shell(command, …)` for user-supplied hook commands. Template substitution uses `shlex.quote` per-argument, but the *outer* shell command is still concatenated and parsed by `/bin/sh`. If config-write is ever attacker-controlled (compromised git, mis-permissioned ConfigMap, malicious PR to a fleet config repo), this is RCE.

**Action.**
1. Replace `create_subprocess_shell` with `create_subprocess_exec` everywhere. The pattern exists in `daemon/workspace_hooks.py:60` already.
2. Ship a `DockerSandbox` backend behind a feature flag. Rootless, network-deny by default, read-only root, tmpfs `/tmp`, seccomp profile.
3. Optional: gVisor / Firecracker for the truly paranoid tier.

---

## 9. (H) Rate Limiting / DoS / Backpressure — **C**

`api/rate_limit.py` implements token bucket + sliding window — clean, well-tested. The implementation comment is the tell:

> "single-process and in-memory. For multi-instance deployments either terminate at the reverse proxy or swap `InMemoryRateLimitStore` for a Redis-backed implementation."

There is no Redis-backed implementation. The fleet story is "scale to N workers", but **the rate limiter scales to 1 worker**, so N workers means an attacker gets an N× multiplier on every limit.

WebSocket connections in `api/server.py` accept an unbounded number per client. The 2026-04 review flagged this; the fix is per-token connection cap + heartbeat eviction.

**Action.** Implement `RedisRateLimitStore` with Lua-script INCR/EXPIRE; cap WS connections per identity; document `--rate-limit-backend redis://...` as the production default.

---

## 10. (I) Observability — **B+**

Genuinely a strength:

- `metrics.py` ships rich Prometheus counters/histograms/gauges.
- `structlog` with correlation IDs in middleware.
- `audit.py` SHA-256 chain with `/api/audit/verify` endpoint.
- `deploy/grafana/maxwell-daemon-dashboard.json` ships a Grafana dashboard.
- `deploy/prometheus/alerts.yml` ships alert rules.
- `docs/operations/monitoring.md` documents it all.

**Gaps.**
- No distributed tracing. OpenTelemetry SDK is a dev dep but not wired — no spans across daemon ↔ backend ↔ sandbox ↔ git boundaries. This is the #1 missing tool when debugging "why was this task slow."
- No SLO doc. P50/P95/P99 targets, error-budget policy, on-call ladder — nothing.
- Audit log lives on local disk only; no forwarding to S3/syslog/SIEM.

**Action.** Wire OTel exporter to OTLP; publish `docs/operations/slo.md` with three SLOs (dispatch latency, task success rate, sandbox availability); ship `audit_sink: s3|syslog|file` config.

---

## 11. (J) Operational Readiness — **B**

Real strengths: multi-stage Dockerfile, non-root runtime, systemd unit with `ProtectSystem=strict / NoNewPrivileges / MemoryDenyWriteExecute / RestrictAddressFamilies`. `core/backup.py` produces a manifest with BLAKE3+SHA-256 over SQLite (Online Backup API) + artifact blobs and excludes secrets.

**Gaps.**
- Backup *restore* is not exercised in CI; a backup you never restore is theatre. Add a `tests/integration/test_backup_restore.py`.
- No runbook. Ops on-call cannot find "what to do when `/api/health` returns degraded."
- Audit rotation is non-atomic — a crash mid-rotate corrupts the chain. Use `os.replace` over a temp file and add a "rotation marker" entry that preserves the prior tail hash.

---

## 12. (K) Scalability — **D+**

The honest ceiling: **one node.** Reasons, in order of severity:

1. SQLite WAL is the single source of truth for tasks, delegates, ledger.
2. Rate limiter is in-process.
3. WebSocket fanout is in-process.
4. Lease recovery assumes a single coordinator.
5. There is no leader election, no quorum, no cluster identity.

This is fine for a single-team appliance; it is **not fine for "autonomous AI control plane"** positioning. The work to fix it is substantial but well-scoped:

- `PersistenceBackend` Protocol with `sqlite` + `postgres` implementations.
- Redis (or Postgres advisory locks) for rate limiting and lease coordination.
- Sticky WS sessions at the LB, with PubSub fanout between nodes.
- ADRs documenting the choices.

`docs/adr/` exists but is sparse; this is the highest-impact place to invest one engineering-week of design work.

---

## 13. (L) API Contract & Documentation — **B**

`/openapi.json`, `/docs`, `/redoc` are auto-served; `/api/version` advertises `CONTRACT_VERSION`; `SPEC.md` declares append-only semantics. That's better than most projects this age.

**Missing.**
- No deprecation policy. "Append-only within a major version" is great until you need to fix a typo in a response key.
- No example clients (Python, JS, curl) in `docs/`. Integration friction.
- No error-code reference table — clients consuming `problem+json` need stable type URIs.
- No Postman / Bruno collection.

---

## 14. (M) UI/UX & Accessibility — **C**

`/ui/` ships:

```
index.html    471 lines
app.js      1,917 lines    (monolithic, no module boundaries)
style.css   1,173 lines
sw.js         106 lines    (service worker — nice touch)
theme.js       23 lines    (system-preference dark mode, no toggle)
```

It works. It's lightweight. It's also the single most visible part of the product to a new evaluator, and right now it looks like an admin panel. Competitors (Cursor, Continue, Copilot Workspace) have invested heavily in UI polish; Maxwell loses that comparison on the demo screenshot, before the user ever sees the Crucible loop.

The sibling `runner-dashboard` repo is the "real" operator console — that's smart architecture but a marketing handicap. The local `/ui/` needs to either (a) get a serious UX pass, or (b) embed `runner-dashboard` as a submodule for parity.

**Concrete gaps.**
- No ARIA roles on most controls; not keyboard-navigable end-to-end.
- No dark-mode toggle (only system preference).
- No mobile / tablet break.
- `app.js` is one file — splitting into modules is a one-week refactor that buys five years of maintainability.

---

## 15. (N) Feature Completeness vs Competitors — **A−**

Maxwell-Daemon's *engine* beats every named competitor on raw capability:

| Capability | Maxwell | Aider | Continue | Cursor | OpenHands | Copilot Workspace |
|---|---|---|---|---|---|---|
| 25+ LLM backends | ✅ | ⚠️ | ✅ | ❌ | ⚠️ | ❌ |
| Adversarial Crucible / critic loop | ✅ | ❌ | ❌ | ❌ | ⚠️ | ❌ |
| Task graphs + typed artifact handoff | ✅ | ❌ | ❌ | ❌ | ⚠️ | ❌ |
| Cost ledger (provider × model × task) | ✅ | ⚠️ | ❌ | ❌ | ❌ | ❌ |
| Policy gauntlets / waivers | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Sandbox isolation | ⚠️ host | ⚠️ host | ⚠️ host | ⚠️ host | ✅ docker | ✅ cloud |
| SSH session pool | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| GitHub-native PR flow | ✅ | ✅ | ⚠️ | ⚠️ | ✅ | ✅ |
| Codebase semantic search | ❌ | ❌ | ✅ | ✅ | ⚠️ | ✅ |
| Polished IDE UX | ⚠️ basic | ❌ | ✅ | ✅ | ❌ | ✅ |
| Polished web dashboard | ⚠️ | ❌ | ❌ | ❌ | ⚠️ | ✅ |

**Where Maxwell wins.** Multi-agent orchestration, cost transparency, vendor-neutrality, policy enforcement. These are the "infra grown-up" features that matter for procurement, not for first-impression demos.

**Where Maxwell loses.** Sandbox isolation (OpenHands ships Docker by default), codebase indexing (Cody/Continue do this natively), demo UX (Cursor wins this every time), IDE depth (Cursor is an IDE; Maxwell has 5 extensions but they're thin).

**Strategic read.** Lean into the moat: *cost ledger + Crucible + multi-backend + policy gates* is a defensible differentiation against the IDE crowd. Don't try to out-IDE Cursor — out-govern them. Position for teams that need audit, budget control, and adversarial QA; that's where the IDE plugins are losing.

---

## 16. (O) Developer Experience & Extensibility — **A−**

The CLI surface is large and clean: `maxwell-daemon serve / dispatch / tasks / task-graph / delegate / gauntlet / action / fleet / memory / work-item / backup / spec / eval / session / doctor`. The MCP registry (`tools/mcp.py`) lets external tools plug in cleanly. 5 IDE extensions (`vscode/`, `conductor-vscode/`, `jetbrains/`, `zed/`, `obsidian/`) plus an `apps/desktop-electron` app.

**Gaps.** No official Python SDK (`pip install maxwell-daemon-client`). No JS/TS SDK for the dashboard ecosystem. IDE extensions are *thin* — they shell out, they don't embed.

---

## 17. The Production Epic — How to Get to A

A 14-week roadmap split into four phases. Each phase is independently shippable. Effort estimates are engineering-weeks (one engineer, full-time).

### Phase 1 — Stop the Bleeding (Weeks 1–3)

| # | Track | Effort | Definition of done |
|---|---|---|---|
| 1.1 | **Decompose `api/server.py`** | 2w | No route in `server.py`; file ≤ 600 lines; 8+ router modules under `api/routes/`; tests green |
| 1.2 | **Typed exception tree + RFC 7807** | 1w | `MaxwellError` hierarchy; single `exception_handler`; 0 bare `except` in HTTP layer; audit-path catches `OSError` only |
| 1.3 | **`hooks.py` → `create_subprocess_exec`** | 2d | No `create_subprocess_shell` in repo; CI guard added |
| 1.4 | **Coverage gate to 80%** | 1d | `--cov-fail-under=80`; failing tests fixed, not relaxed |

**Exit gate:** `ruff check --select BLE,C901` net-negative WoW; no new `# noqa: BLE001` added.

### Phase 2 — Make It Scale (Weeks 4–7)

| # | Track | Effort | Definition of done |
|---|---|---|---|
| 2.1 | **`PersistenceBackend` Protocol + Postgres impl** | 2w | `sqlite` + `postgres` backends behind same interface; full integration test matrix; ADR-001 published |
| 2.2 | **Redis-backed rate limiter + WS cap** | 1w | `RedisRateLimitStore` with Lua INCR/EXPIRE; per-identity WS connection cap; load test proves multi-instance enforcement |
| 2.3 | **Decompose `daemon/runner.py`** | 2w | `Daemon` is composed via DI; file ≤ 600 lines; no direct `sqlite3` imports in `daemon/` or `core/delegate_lifecycle.py` |
| 2.4 | **Lease coordination across nodes** | 1w | Postgres advisory locks (or Redis) for lease acquisition; chaos test: kill one of two nodes, no task lost |

**Exit gate:** 2-node deployment passes a 100-RPS / 10-minute load test with rate-limit headers identical to single-node.

### Phase 3 — Production-Grade Operations (Weeks 8–10)

| # | Track | Effort | Definition of done |
|---|---|---|---|
| 3.1 | **OTel distributed tracing** | 1w | Spans across daemon → backend → sandbox → git; OTLP exporter configurable; `docs/operations/tracing.md` |
| 3.2 | **SLO doc + runbook** | 1w | `docs/operations/slo.md` (3 SLOs, error-budget policy); `docs/operations/runbook.md` (10 scenarios) |
| 3.3 | **Audit forwarding (S3 / syslog)** | 4d | `audit_sink: s3|syslog|file` config; tamper-evidence preserved across forward; atomic rotation |
| 3.4 | **Backup restore in CI** | 3d | `tests/integration/test_backup_restore.py` exercises full restore; runs on every PR |
| 3.5 | **Docker sandbox backend** | 2w | Rootless OCI runner; network deny; seccomp profile; documented threat model |

**Exit gate:** External pen-test (or internal red-team) finds nothing rated High+ in 1-week engagement.

### Phase 4 — Win the Demo (Weeks 11–14)

| # | Track | Effort | Definition of done |
|---|---|---|---|
| 4.1 | **`/ui/` rebuild on React + Vite + Tailwind** | 3w | `app.js` split into modules; Lighthouse a11y ≥ 90, perf ≥ 85; dark mode toggle; mobile/tablet breakpoints |
| 4.2 | **Codebase semantic search** | 2w | Tree-sitter index in `memory/repo_memory.py`; Strategist consumes it; benchmark shows ≥ 20% token reduction |
| 4.3 | **Official Python SDK** | 1w | `maxwell-daemon-client` on PyPI; typed; example notebook |
| 4.4 | **Benchmark leaderboard (SWE-bench Lite)** | 2w | Public results vs Aider/OpenHands; published in `docs/benchmarks/` |

**Exit gate:** First external user can `pip install maxwell-daemon-client`, dispatch a task against SWE-bench, get a passing PR.

### Cross-cutting

- **CI ratchet** (Week 1, ongoing): block new `# noqa: BLE001` and `# noqa: C901` additions.
- **ADR cadence** (Week 1, ongoing): one ADR per architecture decision in Phases 2–3. Target: 8 ADRs by Week 10.
- **Burn-down dashboard**: `# noqa` counts as Prometheus metric, displayed on internal Grafana.

---

## 18. Risk Register

| Risk | P | I | Mitigation |
|---|---|---|---|
| Phase-2 Postgres migration breaks task history | M | H | Dual-write window + shadow-read validation for 1 release |
| `/ui/` rebuild scope creep | H | M | Strict feature-parity scope; defer net-new features to Phase 5 |
| Phase-3 Docker sandbox blocks BYO-CLI users | M | M | Keep host backend behind explicit `--sandbox=host` flag |
| 14-week timeline slips | H | M | Phase boundaries are individually shippable; release v0.2/v0.3/v0.4 at phase boundaries |
| Audit forwarding loses tamper-evidence in transit | L | H | Sign forwarded records with daemon's Ed25519 key; receiver verifies |

---

## 19. Success Metrics — Hold Yourself Accountable

| Metric | Today | Phase 1 | Phase 2 | Phase 3 | Phase 4 |
|---|---|---|---|---|---|
| Largest Python file (LOC) | 3,182 | 600 | 600 | 600 | 600 |
| `# noqa: BLE001` count | ~94 | 60 | 30 | 10 | 0 |
| Integration test count | 5 | 12 | 20 | 28 | 35 |
| Coverage `--cov-fail-under` | 34% | 80% | 82% | 85% | 88% |
| Multi-instance enforced rate limit | ❌ | ❌ | ✅ | ✅ | ✅ |
| Distributed tracing | ❌ | ❌ | ❌ | ✅ | ✅ |
| Docker sandbox available | ❌ | ❌ | ❌ | ✅ | ✅ |
| `/ui/` Lighthouse a11y | unknown | unknown | unknown | unknown | ≥ 90 |
| Published SWE-bench Lite score | none | none | none | none | ✅ |

---

## 20. Closing Read

Maxwell-Daemon is closer to production than the lines-of-code count suggests — most of the unsexy work (audit chain, RBAC, Prometheus, Grafana, systemd hardening, structured logging, Pydantic everywhere) is already done and done well. The remaining work is exactly the work that nobody enjoys: decompose two god-objects, kill the `except Exception` swamp, ship a real persistence interface, and put a UX team on `/ui/`.

The competitive position is stronger than the team probably believes. Cursor and Copilot Workspace are *demo-ware* relative to Maxwell's policy/cost/critic stack. The risk is that procurement teams never see the engine because the demo shows a vanilla-JS panel. The Phase 4 investment in UX and a public benchmark is the cheapest commercial move available.

**Recommended decision:** Commit to the 14-week roadmap as the path to v1.0. Cut every feature request that doesn't sit on the critical path. Ship v0.2 at Phase 1 exit (week 3); v0.3 at Phase 2 (week 7); v0.4 at Phase 3 (week 10); **v1.0 at Phase 4 exit (week 14)**.
