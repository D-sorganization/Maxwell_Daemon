# Deploying with Ansible

The `ansible/` tree contains playbooks for standing up a Conductor fleet on Linux hosts.

## Inventory

Copy the example:

```bash
cp ansible/inventory.example.yml ansible/inventory.yml
$EDITOR ansible/inventory.yml
```

The inventory declares per-host capacity and tags, which feed into the rendered `conductor.yaml` fleet section so every node knows about every other node.

## Install

```bash
ansible-playbook -i ansible/inventory.yml ansible/playbooks/install.yml
```

The playbook:

1. Creates a system user `conductor`.
2. Installs Python 3 + venv.
3. `pip install conductor-agents=={{ conductor_version }}` into a dedicated venv.
4. Renders `/etc/conductor/conductor.yaml` from a Jinja template.
5. Installs a systemd unit with sensible hardening (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`).
6. Enables and starts the `conductor.service`.

Idempotent by design — safe to re-run.

## Upgrading

Bump `conductor_version` in your inventory and re-run:

```bash
ansible-playbook -i ansible/inventory.yml ansible/playbooks/install.yml \
  -e "conductor_version=0.2.0"
```

## Secrets

API keys are **not** baked into the config. The systemd unit relies on environment variables (`ANTHROPIC_API_KEY`, etc.) read at launch time. Use your preferred secrets store (`systemd-creds`, HashiCorp Vault, AWS SSM, etc.) to populate the environment before the service starts.
