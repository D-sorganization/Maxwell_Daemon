# Deployment Guide

Maxwell-Daemon can run as a local developer process, a systemd service managed
by Ansible, or a cloud fleet provisioned with Terraform. Choose the smallest
deployment model that matches the number of workers you need.

## Local Service

Use a local service for single-machine development or workstation automation.

```bash
python -m venv .venv
source .venv/bin/activate
pip install .
maxwell-daemon init
maxwell-daemon health
maxwell-daemon-runner
```

Keep secrets in the environment or your OS secret manager. Do not commit API
keys to `maxwell-daemon.yaml`.

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
