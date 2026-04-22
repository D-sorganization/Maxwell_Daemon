# Tailscale Fleet Deployment

Maxwell-Daemon's fleet transport uses generic HTTP(S) to communicate between the coordinator and workers. While you can run the fleet over any network, **Tailscale** is the recommended topology for securely connecting a distributed fleet of worker nodes.

## Recommended Topology

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

## Security Posture

- **No Public Exposure**: No memory or task APIs should be exposed to the public internet. Ensure your cloud firewalls (Security Groups, VPC rules) block inbound port 8000 from `0.0.0.0/0`.
- **Authentication**: Tailscale provides network-level encryption and identity, but you must still configure a strong `api.auth_token` or JWT configuration in Maxwell-Daemon's configuration. The coordinator and workers will use this token for application-level authentication.
- **ACLs**: Use Tailscale ACLs to restrict which nodes can communicate with the coordinator on port 8000, enforcing least-privilege network access.

## Validating the Setup

Use the `maxwell-daemon health` command on the coordinator, or check the `/api/v1/fleet` endpoint. If the coordinator can reach the workers, their `healthy` status will be `true`. If MagicDNS resolution fails or Tailscale is disconnected, the status will correctly report as unhealthy.
