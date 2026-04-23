# Tailscale fleet deployment

Maxwell-Daemon's fleet transport uses generic HTTP(S) to communicate between the coordinator and workers. While you can run the fleet over any network, **Tailscale** is the recommended topology for securely connecting a distributed fleet of worker nodes.

## Recommended topology

When using Tailscale, deploy your coordinator and all worker nodes onto the same tailnet. This ensures that all fleet coordination traffic is encrypted end-to-end and bypasses the public internet entirely.

- **Coordinator**: Binds its API (e.g., `0.0.0.0:8000`) but is only accessed via its Tailscale IP (e.g., `100.x.y.z`) or MagicDNS name (e.g., `maxwell-coordinator.tailnet-name.ts.net`).
- **Workers**: Connect to the coordinator using the coordinator's MagicDNS name. They do not expose their own APIs to the internet.

> [!NOTE]
> Maxwell-Daemon does **not** install, provision, or manage Tailscale. You must install Tailscale and join the nodes to your tailnet using your preferred infrastructure-as-code tool (e.g., Ansible or Terraform) before configuring the Maxwell fleet.

## Configuration

In your coordinator's `fleet.yaml` (or via `MAXWELL_FLEET_CONFIG`), define the machines using their MagicDNS names:

```yaml
fleet:
  name: "production-fleet"

machines:
  - name: "worker-01"
    host: "worker-01.tailnet-name.ts.net"
    port: 8000
    capacity: 4
  - name: "worker-02"
    host: "worker-02.tailnet-name.ts.net"
    port: 8000
    capacity: 4
```

## Hardening checklist

Treat Tailscale as the private network layer, not the whole security boundary.

- Bind Maxwell only on the interface strategy you intend. `0.0.0.0` is acceptable only when host firewalls and tailnet policy block non-tailnet ingress; otherwise bind to the node's Tailscale address or localhost behind a reverse proxy.
- Never expose `/api/v1/tasks`, `/api/v1/memory/*`, `/api/v1/artifacts/*`, `/api/v1/actions/*`, `/api/v1/work-items/*`, or `/api/v1/ssh/*` to the public internet.
- Block inbound daemon ports from `0.0.0.0/0` and `::/0` in cloud security groups, router port forwarding, and host firewalls.
- Configure a strong `api.auth_token` or JWT configuration. Tailscale gives node identity and transport encryption; Maxwell still needs application-layer authorization.
- Give long-running coordinator and worker nodes Tailscale tags, then grant only the flows the daemon needs.
- Keep user laptops separate from daemon worker tags. Human operators should reach the coordinator dashboard/API through an explicit operator group or admin grant, not by being able to reach every worker.
- Keep secrets out of fleet task prompts and environment variables unless a specific check requires them. Shared memories and artifacts should be considered codebase data, not a secret store.
- Review the redacted fleet capability endpoint before exposing it to users. `/api/v1/fleet/capabilities` intentionally omits raw Tailscale IP and current address details; keep that behavior when adding fields.

## Tailnet policy example

Tailscale's current policy syntax recommends grants for new policy files, while legacy ACLs continue to work. See Tailscale's [policy file syntax](https://tailscale.com/docs/reference/syntax/policy-file), [grants syntax](https://tailscale.com/docs/reference/syntax/grants), and [ACL tag guidance](https://tailscale.com/kb/1068/acl-tags/) before copying this into a real tailnet.

This example keeps worker APIs reachable only from the coordinator on port `8000`, and lets an operator group reach the coordinator on port `8000`. Adapt names, ports, groups, and tag ownership to your tailnet.

```json
{
  "groups": {
    "group:maxwell-operators": ["owner@example.com"]
  },
  "grants": [
    {
      "src": ["tag:maxwell-coordinator"],
      "dst": ["tag:maxwell-worker"],
      "ip": ["tcp:8000"]
    },
    {
      "src": ["tag:maxwell-worker"],
      "dst": ["tag:maxwell-coordinator"],
      "ip": ["tcp:8000"]
    },
    {
      "src": ["group:maxwell-operators"],
      "dst": ["tag:maxwell-coordinator"],
      "ip": ["tcp:8000"]
    }
  ],
  "tagOwners": {
    "tag:maxwell-coordinator": ["autogroup:admin"],
    "tag:maxwell-worker": ["autogroup:admin"]
  },
  "tests": [
    {
      "src": "group:maxwell-operators",
      "accept": ["tag:maxwell-coordinator:8000"],
      "deny": ["tag:maxwell-worker:8000"]
    },
    {
      "src": "tag:maxwell-worker",
      "accept": ["tag:maxwell-coordinator:8000"]
    }
  ]
}
```

Use Tailscale policy `tests` to prevent accidental broadening when you edit the tailnet policy. At minimum, assert that regular operators cannot reach workers directly and that only coordinator/worker tags can use the daemon port.

## Validation commands

Run these from the coordinator after all nodes have joined the tailnet:

```bash
tailscale status
tailscale ping worker-01.tailnet-name.ts.net
curl -fsS http://worker-01.tailnet-name.ts.net:8000/health
curl -fsS -H "Authorization: Bearer $MAXWELL_API_TOKEN" \
  http://worker-01.tailnet-name.ts.net:8000/api/v1/fleet/capabilities?repo=example/repo\&tool=pytest
```

Then verify from a non-worker client that worker APIs are not reachable on the daemon port. If a laptop can call a worker's `/api/v1/tasks` endpoint, the tailnet policy is too broad for background delegation.

Use the `maxwell-daemon health` command on the coordinator, or check the `/api/v1/fleet` endpoint. If the coordinator can reach the workers, their `healthy` status will be `true`. If MagicDNS resolution fails or Tailscale is disconnected, the status will correctly report as unhealthy.
