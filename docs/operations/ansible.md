# Deploying with Ansible

The `deploy/ansible/` tree contains the role-based playbooks for standing up a
Maxwell-Daemon fleet on Linux hosts. It is the single, canonical Ansible tree
(an older flat `ansible/` tree using the pre-rename "conductor" naming was
removed in #986).

## Inventory

Copy the example and fill in real hosts:

```bash
cp deploy/ansible/inventory/fleet.yml.example deploy/ansible/inventory/fleet.yml
$EDITOR deploy/ansible/inventory/fleet.yml
```

The inventory declares the `maxwell_primary` and `maxwell_agents` host groups
plus per-fleet variables (`maxwell_version`, `maxwell_port`,
`maxwell_config_dir`, `maxwell_data_dir`). Role defaults live in
`deploy/ansible/roles/maxwell_daemon/defaults/main.yml`.

## Install

```bash
ansible-playbook -i deploy/ansible/inventory/fleet.yml deploy/ansible/install-maxwell.yml
# pin a version:
ansible-playbook -i deploy/ansible/inventory/fleet.yml deploy/ansible/install-maxwell.yml \
  -e maxwell_version=0.2.0
```

The `maxwell_daemon` role:

1. Creates the `maxwell` system user.
2. Installs Python 3 + venv into `maxwell_venv`.
3. Installs `maxwell-daemon` at `maxwell_version` into that venv.
4. Renders the config and a systemd unit with hardening
   (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`).
5. Enables and starts `maxwell-daemon.service`.

The playbook verifies the service is active and waits for the API health probe.
Idempotent by design — safe to re-run.

## Other playbooks

| Playbook | Purpose |
| --- | --- |
| `install-maxwell.yml` | Install + start the daemon on `maxwell_primary`. |
| `configure-maxwell.yml` | Re-render config / apply settings without a full reinstall. |
| `upgrade-maxwell.yml` | Bump `maxwell_version` and restart. |
| `deploy-agents.yml` | Provision `maxwell_agents` worker hosts. |
| `health-check.yml` | Probe the fleet's API health endpoints. |
| `backup-config.yml` | Back up the rendered config + data dir. |

## Secrets

API keys are **not** baked into the config. The systemd unit relies on
environment variables (`ANTHROPIC_API_KEY`, etc.) read at launch time. Use your
preferred secrets store (`systemd-creds`, HashiCorp Vault, AWS SSM, etc.) to
populate the environment before the service starts.
