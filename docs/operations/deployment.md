# Deployment Guide

Maxwell-Daemon can run as a local developer process, a systemd service managed
by Ansible, or a cloud fleet provisioned with Terraform. Choose the smallest
deployment model that matches the number of workers you need.

## Local Service

Use a local service for single-machine development or workstation automation.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
maxwell-daemon init
maxwell-daemon health
maxwell-daemon-runner
```

Keep secrets in the environment or your OS secret manager. Do not commit API
keys to `maxwell-daemon.yaml`.

## Ansible Fleet

Use Ansible when you already know the hosts that should run workers.

```bash
cp ansible/inventory.example.yml ansible/inventory.yml
$EDITOR ansible/inventory.yml
ansible-playbook -i ansible/inventory.yml ansible/playbooks/install.yml
```

The playbook installs Python, creates a dedicated service user, renders
configuration, installs a hardened systemd unit, and starts the daemon.

Use the playbooks under `deploy/ansible/` for conductor-oriented fleet
operations such as backups, health checks, upgrades, and agent deployment.

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
