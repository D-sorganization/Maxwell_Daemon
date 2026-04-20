# GitHub webhooks

Maxwell-Daemon can react to GitHub activity directly — new issues, comments with trigger phrases — instead of being polled. This closes the loop from "something happens on GitHub" → "daemon drafts a PR" without a human in between.

## How it works

1. GitHub delivers an HTTP POST to `/api/v1/webhooks/github`.
2. Maxwell-Daemon verifies the `X-Hub-Signature-256` HMAC using `hmac.compare_digest` (constant time).
3. The event is matched against your configured routes. If a route matches, a task is queued.
4. The daemon picks the task off the queue and runs the normal issue-execution flow.

Ping events (GitHub's health check) return 200 without dispatching anything.

## Config

```yaml
github:
  webhook_secret: ${GITHUB_WEBHOOK_SECRET}
  allowed_repos:
    - my-org/project-a
    - my-org/project-b
  routes:
    # Plan mode: any new issue with the label `maxwell-daemon-plan`
    - event: issues
      action: opened
      label: maxwell-daemon-plan
      mode: plan

    # Implement mode: new issue labelled `maxwell-daemon-implement`
    - event: issues
      action: opened
      label: maxwell-daemon-implement
      mode: implement

    # Ad-hoc: a maintainer types `/maxwell-daemon plan` in a comment
    - event: issue_comment
      action: created
      trigger: "/maxwell-daemon plan"
      mode: plan

    - event: issue_comment
      action: created
      trigger: "/maxwell-daemon implement"
      mode: implement
```

### Fields

| Key              | Purpose                                                 |
|------------------|---------------------------------------------------------|
| `webhook_secret` | Shared secret with the GitHub webhook (required)        |
| `allowed_repos`  | Whitelist — dispatches only happen for these repos      |
| `event`          | GitHub event name (`issues`, `issue_comment`, …)        |
| `action`         | Event action (`opened`, `closed`, `created`, …)         |
| `label`          | If set, issue must carry this label for the route to fire |
| `trigger`        | If set, comment body must contain this substring        |
| `mode`           | `plan` or `implement`                                   |

## Configuring GitHub

1. Go to your repo → **Settings → Webhooks → Add webhook**.
2. Payload URL: `https://<your-daemon>/api/v1/webhooks/github`
3. Content type: `application/json`
4. Secret: the same value as `github.webhook_secret` in `maxwell-daemon.yaml`
5. Events: subscribe to **Issues** and **Issue comments**. Add more later if your routes grow.
6. Active: ✅

GitHub will send a `ping` event immediately; Maxwell-Daemon will respond 200 and the webhook will show green in the GitHub UI.

## Security

- **Signature verification is mandatory.** Without a configured secret the endpoint returns 404 — we never accept unsigned events.
- **Repo whitelist.** Even a valid-signature event for a non-whitelisted repo is ignored. Stops anyone who gets hold of the secret from dispatching arbitrary repos.
- **Constant-time comparison.** Signature check uses `hmac.compare_digest`.
- **Label + trigger gating.** Drive-by strangers can't start an implement-mode run by opening an issue — they need push access to apply a label or leave a `/maxwell-daemon implement` comment.

## Observability

Every webhook delivery is correlated by the middleware's `X-Request-ID`. Responses carry the same id so you can match a webhook delivery in GitHub's UI back to the daemon log line that handled it.
