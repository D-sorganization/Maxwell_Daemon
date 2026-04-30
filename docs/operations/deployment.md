# Deployment Guide

Maxwell-Daemon can run as a local developer process, a systemd service managed
by Ansible, or a cloud fleet provisioned with Terraform. Choose the smallest
deployment model that matches the number of workers you need.

## Local Service

Use a local service for single-machine development, home-lab automation, or a
single workstation that later dispatches work to other machines.

For a source checkout, prefer the shipped launchers instead of reconstructing
the bootstrap sequence by hand:

| Platform | Launcher |
| --- | --- |
| Windows | `Launch-Maxwell.bat` |
| macOS | `Launch-Maxwell.command` |
| Linux | `Launch-Maxwell.sh` |

Those wrappers all delegate to the same Python entrypoint and perform the same
first-run sequence:

1. Create a local `.venv` inside the checkout when one does not exist yet.
2. Install the runtime package with `pip install -e .`.
3. Create a starter config if the selected config path is missing.
4. Run `maxwell-daemon doctor`.
5. Start `maxwell-daemon serve`.
6. Open the canonical dashboard at `/ui/` unless `--no-open-browser` is set.

If you need the exact wrapper behavior without the platform-specific shell
wrapper, run the launcher module directly from the checkout:

```bash
python -m maxwell_daemon.launcher --repo-root . --port 8080
```

For headless or remote sessions, disable the browser handoff explicitly:

```bash
python -m maxwell_daemon.launcher --repo-root . --port 8080 --no-open-browser
```

Then verify the API surface that the wrapper is expected to bring up:

```bash
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/docs > /dev/null
curl -fsS http://127.0.0.1:8080/ui/ > /dev/null
```

Those checks correspond to `GET /health`, `GET /docs`, and `GET /ui/`.

Keep secrets in the environment or your OS secret manager. Do not commit API
keys to `maxwell-daemon.yaml`.

A successful first boot means the daemon can initialize its local runtime,
write config, and expose the API. It does not mean task execution is fully
ready. If `maxwell-daemon doctor` reports backend warnings, edit the starter
config and provide the required credentials or local model runtime before
dispatching real work.

### Timed fresh deploy proof

Issue #19 required a timed proof that the target home-user bootstrap path can
come up from a fresh source tree in under 30 minutes. The measured proof below
uses the same launcher code path that `Launch-Maxwell.bat` uses on Windows.

On April 23, 2026, Maxwell-Daemon was started from:

- a throwaway source copy with no existing `.venv`
- an isolated `HOME` / `USERPROFILE` / `APPDATA` / `LOCALAPPDATA`
- an empty config directory passed to `--config`
- a clean API port (`8098`)

The proof command was:

```powershell
py -3 -m maxwell_daemon.launcher --repo-root <clean-source-copy> --config <scratch-config> --port 8098
```

Observed result:

| Check | Result |
| --- | --- |
| Fresh local `.venv` created | Yes |
| Starter config written | Yes |
| `maxwell-daemon doctor` completed | Yes |
| `GET /health` | `200` |
| `GET /docs` | `200` |
| Measured ready time | `96.91 seconds` |

That measured first-run path is well under the 30-minute release-readiness
gate. Re-run this proof whenever launcher bootstrap steps, dependency weight,
or starter-config generation changes.

## Ansible Fleet

Use Ansible when you already know the hosts that should run workers. For securely connecting your fleet over a private network, see the [Tailscale Fleet Deployment Guide](tailscale.md).

```bash
cp ansible/inventory.example.yml ansible/inventory.yml
$EDITOR ansible/inventory.yml
ansible-playbook -i ansible/inventory.yml ansible/playbooks/install.yml
```

The playbook installs Python, creates a dedicated service user, renders
configuration, installs a hardened systemd unit, and starts the daemon.

Use the playbooks under `deploy/ansible/` for conductor-oriented fleet
operations such as backups, health checks, upgrades, and agent deployment.

## Tailscale Tailnet Fleet

Maxwell-Daemon does not install, join, or administer Tailscale. It can run over a
tailnet when the coordinator and workers already have Tailscale installed and
authenticated by your normal device-management process.

Use this topology when workers should be reachable only on the private tailnet:

- Join the coordinator and every worker to the same tailnet.
- Address workers by MagicDNS names such as `worker-1.tailnet-name.ts.net` or by
  their stable `100.x.y.z` Tailscale addresses.
- Keep `api.auth_token` enabled on every Maxwell-Daemon API node.
- Avoid public-network exposure for memory, task, or fleet API routes. Bind to a
  Tailscale interface address, a private interface behind host firewall rules, or
  `127.0.0.1` when the process is only accessed through a local proxy.

Example fleet excerpt:

```yaml
fleet:
  discovery_method: manual
  heartbeat_seconds: 30
  machines:
    - name: coordinator
      host: coordinator.tailnet-name.ts.net
      port: 8080
      capacity: 2
      tags: [coordinator]
    - name: gpu-worker-1
      host: gpu-worker-1.tailnet-name.ts.net
      port: 8080
      capacity: 4
      tags: [gpu, tailnet]

api:
  enabled: true
  host: 100.64.12.34
  port: 8080
  auth_token: ${MAXWELL_API_TOKEN}
```

Before dispatching work, run these checks from the coordinator:

```bash
tailscale status
tailscale ping gpu-worker-1.tailnet-name.ts.net
curl -fsS -H "Authorization: Bearer ${MAXWELL_API_TOKEN}" \
  http://gpu-worker-1.tailnet-name.ts.net:8080/health
maxwell-daemon doctor --config ~/.config/maxwell-daemon/maxwell-daemon.yaml
```

If those checks fail, fix Tailscale reachability, firewall policy, or the daemon
service before changing Maxwell-Daemon task routing. Treat Tailscale
provisioning and Maxwell-Daemon transport as separate layers: Tailscale provides
private IP reachability, while Maxwell-Daemon still owns API authentication,
task authorization, and fleet metadata.

## Terraform Infrastructure

Use Terraform when the fleet should be provisioned from scratch.

```bash
cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars
terraform init
terraform plan
terraform apply
```

The Terraform module defines the cloud resources and outputs connection details
that can feed the Ansible inventory or another configuration-management layer.

## Containers and Kubernetes

For container platforms, build an image with the project installed and provide
configuration through mounted files plus environment variables:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install .
CMD ["maxwell-daemon-runner"]
```

In Kubernetes, keep API keys in Secrets, mount configuration as a ConfigMap, and
run separate Deployments for API-serving nodes and worker nodes when you need
different scaling policies.

## Release Checklist

Before promoting a deployment:

- `maxwell-daemon health` passes on every backend expected to serve traffic.
- `/health` is reachable from the load balancer or service monitor.
- `/metrics` is scraped by Prometheus or an equivalent collector.
- Budget thresholds are configured for paid backends.
- Logs include task lifecycle events but do not include secrets.
- Rollback instructions are documented for the selected deployment path.

## Production Operations

The remainder of this page is the operator-facing guide for running
Maxwell-Daemon in production: requirements, install paths, environment
variables, filesystem layout, reverse-proxy configuration, systemd,
health probes, SQLite backup/restore, and the upgrade procedure.

For the metrics catalogue and alert rule reference, see
[`monitoring.md`](monitoring.md).

### System requirements

- Python `>=3.10` (declared in [`pyproject.toml`](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/pyproject.toml)).
- POSIX-like filesystem capable of holding the SQLite WAL files (Linux,
  macOS, or WSL2).  Pure NFS is not recommended — SQLite WAL relies on
  byte-range locking that some NFS implementations handle poorly.
- Outbound HTTPS to whichever LLM providers you enable
  (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.).  All other dependencies
  are pulled in by `pip install`.
- A writable data directory (default: `~/.maxwell` for source checkouts,
  `/var/lib/maxwell-daemon` for the systemd layout below).

Capacity sizing is workload-specific.  As a starting point, allocate
1 vCPU and 1 GiB RAM per concurrent task; the SQLite ledger grows ~1 KB
per agent request and should not exceed a few GiB even at heavy use.

### Install

#### From PyPI / pip

```bash
python -m venv /opt/maxwell-daemon/.venv
/opt/maxwell-daemon/.venv/bin/pip install --upgrade pip
/opt/maxwell-daemon/.venv/bin/pip install maxwell-daemon
```

The `maxwell-daemon` console script is installed into the venv's `bin/`
directory.  Verify with `/opt/maxwell-daemon/.venv/bin/maxwell-daemon --version`.

#### From source

```bash
git clone https://github.com/D-sorganization/Maxwell_Daemon.git
cd Maxwell_Daemon
python -m venv .venv
.venv/bin/pip install -e .
```

For a developer-style install with extra tooling:

```bash
.venv/bin/pip install -e ".[dev]"
```

Production installs should pin to a tagged release rather than tracking
`main`.

#### Docker

The repo ships a [`Dockerfile`](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/Dockerfile) and
[`docker-compose.yml`](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/docker-compose.yml).
Build and run with:

```bash
docker build -t maxwell-daemon:local .
docker run --rm -p 8080:8080 \
  -v $HOME/.config/maxwell-daemon:/root/.config/maxwell-daemon:ro \
  -v maxwell-workspace:/workspace \
  --env-file .env \
  maxwell-daemon:local
```

Or via Docker Compose, which already wires the workspace volume, config
mount, and `/api/health` healthcheck:

```bash
docker compose up -d
docker compose logs -f maxwell-daemon
```

### Environment variables

Maxwell-Daemon reads these `MAXWELL_*` variables directly from the
running code.  The list below is exhaustive for the daemon itself; LLM
backend keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.) are documented
in [`.env.example`](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/.env.example).

| Variable | Default | Source | Purpose |
|----------|---------|--------|---------|
| `MAXWELL_CONFIG` | XDG default | `maxwell_daemon/config/loader.py` | Path to the main `config.toml`. |
| `MAXWELL_FLEET_CONFIG` | `fleet.yaml` in cwd | `maxwell_daemon/config/fleet.py` | Path to the fleet manifest. |
| `MAXWELL_API_TOKEN` | unset | `maxwell_daemon/cli/work_items.py` | Bearer token used by the CLI when calling the daemon's HTTP API. |
| `MAXWELL_DAEMON_URL` | unset | `maxwell_daemon/cli/work_items.py` | Base URL the CLI uses to reach the daemon. |
| `MAXWELL_ALLOW_ENV` | empty | `maxwell_daemon/tools/builtins.py` | Comma-separated allow-list of env vars exposed to sandboxed tools. |
| `MAXWELL_REDACT_LOGS` | `1` | `maxwell_daemon/logging.py` | Set to `0` to disable secret redaction in logs (do not do this in production). |
| `MAXWELL_AGGRESSIVE_COMPRESSION` | unset | `maxwell_daemon/core/repo_overrides.py` | Set to `1` to enable aggressive context compression. |
| `MAXWELL_CONTRACTS` | `on` | `maxwell_daemon/contracts.py` | Set to `off` to disable design-by-contract enforcement. |
| `MAXWELL_RATELIMIT_DEFAULT_PER_MIN` | `120` | `maxwell_daemon/api/rate_limit.py` | Per-IP request budget for read endpoints. |
| `MAXWELL_RATELIMIT_WRITE_PER_MIN` | `30` | `maxwell_daemon/api/rate_limit.py` | Per-IP request budget for write endpoints. |

### Filesystem layout

The systemd layout in this guide assumes:

```
/opt/maxwell-daemon/                  # install root
├── .venv/                            # virtualenv created by the operator
└── bin/maxwell-daemon                # console script (symlink into .venv)

/etc/maxwell-daemon.env               # EnvironmentFile for the systemd unit
/etc/maxwell-daemon/                  # operator-managed config
└── config.toml

/var/lib/maxwell-daemon/              # WorkingDirectory + ReadWritePaths
├── tasks.db                          # task store (SQLite, WAL mode)
├── tasks.db-wal                      # WAL frames
├── tasks.db-shm                      # shared-memory index
└── ledger.db                         # cost ledger (SQLite, WAL mode)

/var/log/maxwell-daemon/              # rotating structlog JSON output
└── app.log
```

For source checkouts using the launchers, the equivalent paths default
to `~/.config/maxwell-daemon/` for config and `~/.maxwell/` for the
ledger and task store.

### Reverse proxy (nginx)

Terminate TLS at nginx and forward to the daemon over loopback.  The
config below preserves client IPs and upgrades the `/api/v1/events`
WebSocket connection.

```nginx
upstream maxwell_daemon {
    server 127.0.0.1:8080;
    keepalive 32;
}

server {
    listen 443 ssl http2;
    server_name maxwell.example.com;

    ssl_certificate     /etc/letsencrypt/live/maxwell.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/maxwell.example.com/privkey.pem;

    # Generous timeout for long-running agent requests.
    proxy_read_timeout  600s;
    proxy_send_timeout  600s;
    client_max_body_size 16m;

    location / {
        proxy_pass http://maxwell_daemon;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket upgrade for /api/v1/events
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # Keep /metrics on an internal listener if you don't want it
    # publicly reachable.  This block forbids external scrapes.
    location = /metrics {
        allow 10.0.0.0/8;
        allow 127.0.0.1;
        deny all;
        proxy_pass http://maxwell_daemon;
    }
}
```

### systemd

A reference unit lives at
[`deploy/systemd/maxwell-daemon.service`](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/deploy/systemd/maxwell-daemon.service).
Install it with:

```bash
sudo useradd --system --home-dir /var/lib/maxwell-daemon --shell /usr/sbin/nologin maxwell
sudo install -d -o maxwell -g maxwell /var/lib/maxwell-daemon /var/log/maxwell-daemon
sudo install -m 0644 deploy/systemd/maxwell-daemon.service /etc/systemd/system/maxwell-daemon.service
sudo install -m 0640 -o root -g maxwell /dev/null /etc/maxwell-daemon.env
$EDITOR /etc/maxwell-daemon.env   # populate MAXWELL_CONFIG, API keys, etc.
sudo systemctl daemon-reload
sudo systemctl enable --now maxwell-daemon
sudo systemctl status maxwell-daemon
```

Hot reload of config is not supported; restart the unit after editing
`/etc/maxwell-daemon/config.toml`.

### Health probes

Three first-party endpoints are intended for orchestrators:

| Endpoint | Use as | Notes |
|----------|--------|-------|
| `GET /health` | Liveness | Cheapest possible probe; returns immediately. |
| `GET /api/health` | Liveness | Reports gate state too — preferred for Kubernetes `livenessProbe`. |
| `GET /api/status` | Readiness | Pipeline state and active task summary; safe for `readinessProbe`. |
| `GET /api/version` | Contract | Semver + contract version (used for upgrade checks). |

`/api/health` and `/api/version` are exempted from the env-driven rate
limiter (Phase 1 of #796) by default, so orchestrator probes will not
trigger 429s. Deployments that explicitly enable the YAML-configured
limiter via `api.rate_limit_default` should add these paths to its
`exempt_paths` list — `install_rate_limiter()` currently defaults its
exemption list to `/health` and `/metrics` only. See
[`monitoring.md`](monitoring.md) for the full breakdown; a follow-up
under #796 will align that limiter's defaults.

### SQLite backup and restore

Maxwell-Daemon's SQLite databases (task store and cost ledger) run in
WAL mode.  Do not copy the `.db` file with `cp` — you will get a
torn snapshot.  Instead use the SQLite online backup API:

```bash
# Create a consistent point-in-time backup.
sqlite3 /var/lib/maxwell-daemon/tasks.db ".backup '/var/backups/maxwell/tasks-$(date +%F).db'"
sqlite3 /var/lib/maxwell-daemon/ledger.db ".backup '/var/backups/maxwell/ledger-$(date +%F).db'"
```

This is safe to run while the daemon is live; the backup command takes
the appropriate locks and copies the database in pages.

A simple cron entry for daily backups with 14-day retention:

```cron
15 3 * * * maxwell sqlite3 /var/lib/maxwell-daemon/tasks.db  ".backup '/var/backups/maxwell/tasks-$(date +\%F).db'" && find /var/backups/maxwell -name 'tasks-*.db'  -mtime +14 -delete
20 3 * * * maxwell sqlite3 /var/lib/maxwell-daemon/ledger.db ".backup '/var/backups/maxwell/ledger-$(date +\%F).db'" && find /var/backups/maxwell -name 'ledger-*.db' -mtime +14 -delete
```

To restore, stop the daemon, replace the `.db` files (and remove any
stale `*-wal` / `*-shm` companions), then restart:

```bash
sudo systemctl stop maxwell-daemon
sudo -u maxwell cp /var/backups/maxwell/tasks-2026-04-30.db /var/lib/maxwell-daemon/tasks.db
sudo -u maxwell rm -f /var/lib/maxwell-daemon/tasks.db-wal /var/lib/maxwell-daemon/tasks.db-shm
sudo systemctl start maxwell-daemon
```

The cost ledger is append-only for audit integrity; restoring from
backup will roll back recorded spend to the backup timestamp.  Reconcile
against your provider's billing dashboard if that gap matters.

### Upgrade procedure

Maxwell-Daemon advertises an HTTP contract version at `GET /api/version`.
The contract is **append-only within a major version** (see
[`SPEC.md`](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/SPEC.md)
and [`CLAUDE.md`](https://github.com/D-sorganization/Maxwell-Daemon/blob/main/CLAUDE.md)).
Use that endpoint to detect breaking upgrades before rolling them out.

1. **Snapshot the database.**
   ```bash
   sqlite3 /var/lib/maxwell-daemon/tasks.db  ".backup '/var/backups/maxwell/tasks-pre-upgrade.db'"
   sqlite3 /var/lib/maxwell-daemon/ledger.db ".backup '/var/backups/maxwell/ledger-pre-upgrade.db'"
   ```
2. **Record the current contract version.**
   ```bash
   curl -fsS http://127.0.0.1:8080/api/version | tee /tmp/maxwell-version-before.json
   ```
3. **Stop the daemon and upgrade the package.**
   ```bash
   sudo systemctl stop maxwell-daemon
   sudo -u maxwell /opt/maxwell-daemon/.venv/bin/pip install --upgrade maxwell-daemon
   sudo systemctl start maxwell-daemon
   ```
4. **Verify the contract version.**
   ```bash
   curl -fsS http://127.0.0.1:8080/api/version | tee /tmp/maxwell-version-after.json
   ```
   Confirm the major version number has not changed.  If it has, audit
   the dashboard and CLI integrations against the new contract before
   declaring the upgrade successful.
5. **Smoke-test the API.**
   ```bash
   curl -fsS http://127.0.0.1:8080/api/health
   curl -fsS http://127.0.0.1:8080/api/status
   ```
6. **Roll back** by reinstalling the previous version and restoring the
   pre-upgrade database snapshot if smoke tests fail.
