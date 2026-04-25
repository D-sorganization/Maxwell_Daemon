# Remote Access & Mobile Monitoring

Maxwell Daemon binds to `127.0.0.1` by default to prevent unauthorized access. If you want to view the daemon UI on your phone or check on agents while away from your desk, you need to expose the local API securely.

## ⚠️ Security Warning

**Never expose the Maxwell Daemon directly to the public internet without authentication.** 
The daemon has the ability to execute code, run commands, and modify your filesystem. If exposed without authentication, an attacker could take complete control of your host machine.

The daemon explicitly refuses to bind to non-loopback interfaces (e.g. `0.0.0.0`) unless `api.jwt_secret` is configured in `maxwell-daemon.yaml`.

## 1. Tailscale + Tailscale Funnel (Recommended)

Tailscale provides the easiest way to access the daemon from your mobile device.

1. Install [Tailscale](https://tailscale.com/) on your host machine and your mobile device.
2. In `~/.config/maxwell-daemon/maxwell-daemon.yaml`, configure JWT:
   ```yaml
   api:
     host: 127.0.0.1
     port: 8080
     jwt_secret: "your-generated-secret-here"
   ```
3. Expose the port to your Tailnet:
   ```bash
   # This exposes it only to devices on your Tailnet
   tailscale serve --bg 8080
   ```
4. Access via your machine's Tailscale IP or MagicDNS name (e.g., `http://my-desktop:8080/ui/`).

If you want to share it publicly (not recommended without strong passwords):
```bash
tailscale funnel --bg 8080
```

## 2. Cloudflare Tunnel

For a permanent public URL (e.g., `https://maxwell.yourdomain.com`), you can use `cloudflared`.

1. Install `cloudflared`.
2. Authenticate and create a tunnel:
   ```bash
   cloudflared tunnel login
   cloudflared tunnel create maxwell
   ```
3. Route traffic to the daemon:
   ```bash
   cloudflared tunnel route dns maxwell maxwell.yourdomain.com
   ```
4. Run the tunnel:
   ```bash
   cloudflared tunnel run --url http://127.0.0.1:8080 maxwell
   ```

*Note: Ensure you have `jwt_secret` configured since the tunnel exposes the daemon to the internet.*

## 3. SSH Tunnel

If you don't want to install extra software or configure JWT, you can use a local SSH tunnel from your client device.

On your remote device (e.g., a laptop):
```bash
ssh -L 8080:127.0.0.1:8080 user@your-desktop-ip
```
Then navigate to `http://127.0.0.1:8080/ui/` in your local browser.

## Progressive Web App (PWA)

The Maxwell Daemon UI is a Progressive Web App. You can install it on your mobile device for a native-like experience:

- **iOS Safari**: Tap the Share button, then select "Add to Home Screen".
- **Android Chrome**: Tap the menu (three dots), then select "Install app" or "Add to Home screen".

Once installed, it will run in standalone mode without browser UI and supports Web Push notifications for approvals and task completions.
