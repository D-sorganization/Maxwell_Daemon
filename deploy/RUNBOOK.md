# Deployment Runbook

## Deployment Paths

| Path | When to use | Guide |
| --- | --- | --- |
| Ansible playbook | Known hosts, repeatable | `ansible/inventory.yml` + `ansible/playbooks/install.yml` |
| WSL2 systemd (manual) | New fleet node, no Ansible | `docs/operations/wsl2-node-deployment.md` |
| `deploy-og-laptop.sh` | OG-Laptop combined deploy | Script in repo root or `~/deploy-og-laptop.sh` |
| Launcher scripts | Local dev / single machine | `Launch-Maxwell.sh` / `.bat` / `.command` |

## Quick Deploy — WSL2 Fleet Node

```bash
cd ~/Linux_Repositories/Maxwell-Daemon
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip -q && pip install -e . -q
deactivate
mkdir -p ~/.config/maxwell-daemon ~/.local/share/maxwell-daemon
# Write config — see docs/operations/wsl2-node-deployment.md Step 3
# Install systemd unit — see Step 5
sudo systemctl daemon-reload
sudo systemctl enable --now maxwell-daemon
curl -s http://localhost:8080/health | python3 -m json.tool
```

## Pre-flight Checklist

Before deploying to any machine, verify:

- [ ] systemd is enabled in WSL (`pidof systemd`)
- [ ] Python 3.10+ available (`python3 --version`)
- [ ] Claude Code CLI installed (`claude --version`) — required for `claude-code-cli` backend
- [ ] Repo is at the canonical flat path (`~/Linux_Repositories/Maxwell-Daemon/`)
- [ ] No stale `.venv` from a previous path or platform
- [ ] `~/.local/share/maxwell-daemon/` directory exists
- [ ] Config file passes validation: run `maxwell-daemon doctor --config ~/.config/maxwell-daemon/maxwell-daemon.yaml`

## Rollback

```bash
# Stop the service
sudo systemctl stop maxwell-daemon

# If venv is broken, recreate
cd ~/Linux_Repositories/Maxwell-Daemon
rm -rf .venv
python3 -m venv .venv && source .venv/bin/activate && pip install -e . && deactivate

# Restore config from backup (if available)
cp ~/.config/maxwell-daemon/maxwell-daemon.yaml.bak ~/.config/maxwell-daemon/maxwell-daemon.yaml

# Restart
sudo systemctl start maxwell-daemon
```

## Common Fixes

See `docs/operations/troubleshooting.md` for detailed symptom/fix tables.
See `docs/operations/wsl2-node-deployment.md` for configuration gotchas.
