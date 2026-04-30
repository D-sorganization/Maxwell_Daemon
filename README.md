# ⚡ Maxwell-Daemon

> **Your Autonomous Software Engineering Team in a Box.**

Maxwell-Daemon is a professionally packaged, autonomous local control plane that orchestrates an entire AI development team to build, test, and ship your software.

Unlike existing tools that are locked to the terminal, Maxwell-Daemon ships a browser-served **gate-aware dashboard** at `/ui/`, strict **Test-Driven Development (TDD)** enforcement, and **Bring-Your-Own-CLI (BYO-CLI)** flexibility. It ensures you never burn an API token on tasks you don't need to.

## Sibling repos

Maxwell-Daemon is the **AI control plane** in a three-repo fleet. The
cross-repo contract is in
[`Repository_Management/docs/sibling-repos.md`](https://github.com/D-sorganization/Repository_Management/blob/main/docs/sibling-repos.md).

| Repo | Role |
| --- | --- |
| [`Repository_Management`](https://github.com/D-sorganization/Repository_Management) | Fleet orchestrator (CI workflows, skills, templates, agent coordination). |
| [`runner-dashboard`](https://github.com/D-sorganization/runner-dashboard) | Operator console; its **Maxwell tab** consumes the daemon's HTTP API. |
| `Maxwell-Daemon` (here) | Strategist / Implementer / Crucible pipeline + ExecutionSandbox + BYO-CLI runtime. |

The daemon's **`/ui/`** is the daemon's own console (for direct/local use).
The fleet-wide operator console is `runner-dashboard`. The daemon never
calls back into the dashboard or Repository_Management — all cross-repo
traffic is into the daemon.

## 🚀 Why Maxwell-Daemon?
- **Canonical Dashboard Launcher**: Use `Launch-Maxwell.bat`, `Launch-Maxwell.command`, or `Launch-Maxwell.sh` from a source checkout to bootstrap Maxwell-Daemon and open the shipped `/ui/` dashboard on Windows, macOS, or Linux.
- **The Cognitive Pipeline**: A state-machine orchestrated team:
  - 🧠 **Strategist**: Formulates architectural plans using the compressed `RepoSchematic`.
  - 💻 **Implementer**: Generates code and runs validation through a policy-gated `ExecutionSandbox`.
  - ⚔️ **Maxwell Crucible**: Adversarial QA role that violently tests the Implementer's code against the Strategist's contract.
- **BYO-CLI**: Don't pay double API taxes. Maxwell-Daemon can hook into your existing local CLI subscriptions (like `jules-cli`, `claude-code`, or `ollama`).

## 📥 Installation & Setup

**Prerequisites:**
- Python 3.10+

**1. Clone the Repository**
```bash
git clone https://github.com/D-sorganization/Maxwell-Daemon.git
cd Maxwell-Daemon
```

**2. Launch the Application**

Use the launcher for your platform:

| Platform | Launcher |
|----------|----------|
| Windows | `Launch-Maxwell.bat` |
| macOS | `Launch-Maxwell.command` |
| Linux | `Launch-Maxwell.sh` |

The launchers create a local `.venv`, install the runtime package without
developer extras, create a starter config if needed, run `maxwell-daemon
doctor`, start `maxwell-daemon serve`, and open the canonical dashboard at
`http://127.0.0.1:8080/ui/` by default.

**Developer setup**
Contributors can install the development extras explicitly:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

## 📖 API Documentation

When the daemon is running locally, an interactive OpenAPI 3 reference is
served alongside the API:

| URL | Purpose |
| --- | --- |
| `http://127.0.0.1:8080/docs` | Swagger UI — explore and try endpoints. |
| `http://127.0.0.1:8080/redoc` | ReDoc — browseable reference. |
| `http://127.0.0.1:8080/openapi.json` | Raw OpenAPI 3 schema (machine-readable). |

The schema is auto-generated from the FastAPI route definitions and the
Pydantic models in [`maxwell_daemon/api/contract.py`](maxwell_daemon/api/contract.py),
so it always tracks the live code. The contract version
(`CONTRACT_VERSION`) is advertised at `GET /api/version` and follows
**append-only** semantics within a major version — see
[`SPEC.md`](SPEC.md) for details.

## 🚢 Deploying to production

Operator-facing guides live under [`docs/operations/`](docs/operations/):

- [`deployment.md`](docs/operations/deployment.md) — system requirements,
  install paths (pip / source / Docker), the `MAXWELL_*` environment
  variable reference, filesystem layout, an nginx reverse-proxy snippet
  with WebSocket upgrade and `X-Forwarded-For`, the systemd unit at
  [`deploy/systemd/maxwell-daemon.service`](deploy/systemd/maxwell-daemon.service),
  health probes, SQLite backup/restore under WAL, and the upgrade
  procedure with the `/api/version` contract check.
- [`monitoring.md`](docs/operations/monitoring.md) — Prometheus metrics
  catalogue, structlog field reference, the starter Grafana dashboard
  ([`deploy/grafana/maxwell-daemon-dashboard.json`](deploy/grafana/maxwell-daemon-dashboard.json)),
  and the starter alert rules
  ([`deploy/prometheus/alerts.yml`](deploy/prometheus/alerts.yml)).
- [`observability.md`](docs/operations/observability.md) — developer-side
  logging API and the `/api/v1/events` WebSocket schema.

## 🧠 Architectural Highlights
- **RepoSchematic**: Generates highly compressed file-and-symbol trees, saving massive token budgets compared to dumping raw files.
- **Memory Annealer**: Automatically compresses verbose agent logs into dense `architectural_state.md` files, responsibly purging raw logs to save disk space.
- **Execution Sandbox**: Validation commands run through an argv allowlist, workspace-root check, environment filter, timeout, output redaction, and artifact capture. The current executor uses host subprocesses; it does not provide Docker, filesystem, network, process, or resource isolation. See [Security](docs/operations/security.md) before running untrusted generated code.

---
**License**: MIT © D-sorganization
