# Deploying with Ansible

The `ansible/` tree contains playbooks for standing up a Maxwell-Daemon fleet on Linux hosts.

## Inventory

Copy the example:

```bash
cp ansible/inventory.example.yml ansible/inventory.yml
$EDITOR ansible/inventory.yml
```

The inventory declares per-host capacity and tags, which feed into the rendered `maxwell-daemon.yaml` fleet section so every node knows about every other node.

## Install

```bash
ansible-playbook -i ansible/inventory.yml ansible/playbooks/install.yml
```

The playbook:

1. Creates a system user `maxwell-daemon`.
2. Installs Python 3 + venv.
3. `pip install maxwell-daemon=={{ maxwell_daemon_version }}` into a dedicated venv.
4. Renders `/etc/maxwell-daemon/maxwell-daemon.yaml` from a Jinja template.
5. Installs a systemd unit with sensible hardening (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`).
6. Enables and starts the `maxwell-daemon.service`.

Idempotent by design — safe to re-run.

## Upgrading

Bump `maxwell_daemon_version` in your inventory and re-run:

```bash
ansible-playbook -i ansible/inventory.yml ansible/playbooks/install.yml \
  -e "maxwell_daemon_version=0.2.0"
```

## Secrets

API keys are **not** baked into the config. The systemd unit relies on environment variables (`ANTHROPIC_API_KEY`, etc.) read at launch time. Use your preferred secrets store (`systemd-creds`, HashiCorp Vault, AWS SSM, etc.) to populate the environment before the service starts.
