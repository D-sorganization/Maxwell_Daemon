# WSL2 Node Deployment (systemd)

Deploy Maxwell-Daemon as a systemd service on a WSL2 machine in the
D-sorganization fleet. This guide covers the bare-metal steps that the
Ansible playbook handles automatically — use it when Ansible isn't available
or when bootstrapping a new fleet node by hand.

This guide was written after the OG-Laptop deployment on 2026-04-27 and
captures every issue encountered during that process.

## Prerequisites

| Requirement | Check command |
| --- | --- |
| WSL2 with Ubuntu 22.04+ | `wsl -l -v` (from PowerShell) |
| systemd enabled | `pidof systemd` (nonzero PID) |
| Python 3.10+ | `python3 --version` |
| Claude Code CLI installed | `claude --version` |
| Git | `git --version` |
| Tailscale joined to tailnet | `tailscale status` |

If systemd is not running, add to `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

Then restart WSL: `wsl --shutdown` from PowerShell.

## Filesystem Layout

Per the fleet filesystem conventions in `AGENTS.md` and `fleet_manifest.yaml`,
repos live in a flat structure:

```
~/Linux_Repositories/
├── Maxwell-Daemon/          # ← NOT nested in a wrapper dir
├── runner-dashboard/
├── Repository_Management/
└── Worktrees/
```

**Critical:** If a repo was previously at a nested path like
`~/Linux_Repositories/Linux_Maxwell-Daemon/Maxwell-Daemon/`, moving it
invalidates the Python virtual environment. See "Virtual Environment
Recovery" below.

## Step 1: Clone or Verify the Repo

```bash
cd ~/Linux_Repositories
git clone git@github.com:D-sorganization/Maxwell-Daemon.git
cd Maxwell-Daemon
git checkout main && git pull
```

## Step 2: Create a Linux Virtual Environment

If the `.venv` was carried over from Windows or from a different directory
path, **delete it entirely and recreate**:

```bash
cd ~/Linux_Repositories/Maxwell-Daemon

# Detect and remove stale venvs
if [ -d .venv ]; then
    SHEBANG=$(head -1 .venv/bin/pip 2>/dev/null || echo "")
    if echo "$SHEBANG" | grep -qiE 'windows|C:\\|Linux_Maxwell-Daemon'; then
        echo "Stale venv detected — removing"
        rm -rf .venv
    fi
fi

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip --quiet
pip install -e . --quiet
deactivate
```

### Why venvs break after repo moves

When `pip install -e .` creates console scripts (like the `maxwell-daemon`
command), it writes the absolute path to the venv's Python interpreter into
the script's shebang line. If you move the directory, the shebang still
points to the old path. The only fix is to delete and recreate the venv.

## Step 3: Write the Configuration

Maxwell-Daemon validates its config with Pydantic and rejects unknown fields.
The minimal valid config for a fleet node using Claude Code CLI auth:

```bash
mkdir -p ~/.config/maxwell-daemon

cat > ~/.config/maxwell-daemon/maxwell-daemon.yaml << 'EOF'
version: "1"

api:
  enabled: true
  host: 127.0.0.1
  port: 8080

backends:
  claude:
    enabled: true
    type: claude-code-cli
    model: claude-sonnet-4-6

agent:
  default_backend: claude

fleet:
  discovery_method: manual
  heartbeat_seconds: 30
  machines: []
EOF
```

### Configuration gotchas

These are the issues encountered during the OG-Laptop deployment, each of
which caused the daemon to fail at startup:

| Mistake | Error | Fix |
| --- | --- | --- |
| Missing `version: "1"` | Pydantic validation error | Always include `version` |
| Backend key name ≠ `agent.default_backend` | `default_backend 'X' not found in backends` | Keys must match |
| Missing `type` or `model` in backend | `Field required` | Always include both |
| `host: 0.0.0.0` without `jwt_secret` | `Refusing to bind to 0.0.0.0 without JWT` | Use `127.0.0.1` or set JWT |
| `logging:` as a top-level key | `Extra inputs are not permitted` | Remove it; use `MAXWELL_LOG_LEVEL` env var |
| Using `type: anthropic` | Backend name is `claude`, not `anthropic` | Use `type: claude` or `type: claude-code-cli` |

### Authentication: Claude Code CLI vs API Key

The `claude-code-cli` backend type (`type: claude-code-cli`) shells out to
the locally installed `claude` CLI binary. Authentication is delegated to the
CLI's own OAuth flow — no API key is needed. This is the recommended
approach for fleet nodes where Claude Code is installed.

If Claude Code is not installed, use the `claude` backend type with an API
key stored in the OS keyring:

```yaml
backends:
  claude:
    type: claude
    model: claude-sonnet-4-6
    api_key_secret_ref: maxwell-daemon/backends/claude/api_key
```

Set the key in the keyring:

```bash
source .venv/bin/activate
python -c "
from maxwell_daemon.secrets import KeyringSecretStore
s = KeyringSecretStore()
s.set('maxwell-daemon/backends/claude/api_key', 'sk-ant-...')
"
```

## Step 4: Create Data Directories

The daemon writes its ledger database to `~/.local/share/maxwell-daemon/`.
This must exist before the service starts, and the systemd unit must be
allowed to write there:

```bash
mkdir -p ~/.local/share/maxwell-daemon
```

## Step 5: Install the systemd Service

```bash
MAXWELL_DIR="$HOME/Linux_Repositories/Maxwell-Daemon"
MAXWELL_CONFIG_DIR="$HOME/.config/maxwell-daemon"
MAXWELL_DATA_DIR="$HOME/.local/share/maxwell-daemon"
VENV_DIR="${MAXWELL_DIR}/.venv"

sudo tee /etc/systemd/system/maxwell-daemon.service > /dev/null <<SVCEOF
[Unit]
Description=Maxwell-Daemon agent orchestrator ($(hostname))
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${MAXWELL_DIR}
ExecStart=${VENV_DIR}/bin/maxwell-daemon serve --config ${MAXWELL_CONFIG_DIR}/maxwell-daemon.yaml
Restart=on-failure
RestartSec=5
Environment=HOME=${HOME}
Environment=PATH=${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin

# Load API keys from a secrets file (optional with claude-code-cli backend)
EnvironmentFile=-${MAXWELL_CONFIG_DIR}/env

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${MAXWELL_DIR} ${MAXWELL_CONFIG_DIR} ${MAXWELL_DATA_DIR} /tmp

[Install]
WantedBy=multi-user.target
SVCEOF
```

### ReadWritePaths — critical detail

The Ansible template (`maxwell-daemon.service.j2`) uses `ProtectHome=true`
with explicit `ReadWritePaths` for the data and log directories. When
writing the unit file by hand, you **must** include
`~/.local/share/maxwell-daemon` (or whatever `memory.workspace_path`
resolves to) in `ReadWritePaths`. Without it, the ledger database cannot be
created and the daemon crashes with `OperationalError: unable to open
database file`.

## Step 6: Enable and Start

```bash
sudo systemctl daemon-reload
sudo systemctl enable maxwell-daemon.service
sudo systemctl restart maxwell-daemon
sleep 3
sudo systemctl status maxwell-daemon --no-pager
curl -s http://localhost:8080/health | python3 -m json.tool
```

Expected health response:

```json
{
    "status": "ok",
    "version": "0.1.0",
    "uptime_seconds": 3.2
}
```

## Troubleshooting

### Service exits with status=226/NAMESPACE

The systemd sandboxing cannot access the `WorkingDirectory` or a path in
`ReadWritePaths`. Verify the paths exist and are spelled correctly in the
unit file:

```bash
cat /etc/systemd/system/maxwell-daemon.service | grep -E 'WorkingDirectory|ReadWritePaths|ExecStart'
ls -la ~/Linux_Repositories/Maxwell-Daemon/.venv/bin/maxwell-daemon
```

### Service exits with status=203/EXEC

The `ExecStart` binary doesn't exist or has a stale shebang. Recreate the
venv (Step 2).

### Service exits with status=1/FAILURE

Check the journal for the Python traceback:

```bash
sudo journalctl -u maxwell-daemon --since "5 min ago" --no-pager | tail -30
```

Common causes: config validation errors (see the gotchas table above) or
missing data directory (Step 4).

### Stale venv after repo migration

If the repo was moved from one path to another, all scripts in `.venv/bin/`
have hardcoded shebangs to the old path. Symptoms: `bad interpreter: No such
file or directory`. Fix: `rm -rf .venv && python3 -m venv .venv && source
.venv/bin/activate && pip install -e .`
