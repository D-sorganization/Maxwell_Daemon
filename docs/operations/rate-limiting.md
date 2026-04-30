# Rate Limiting

Maxwell-Daemon ships an opt-in, per-endpoint rate limiter that protects the
control-plane API from runaway clients. Phase 1 (this document) covers
`POST /api/dispatch` only — the highest-risk endpoint, since each successful
call enqueues an LLM-backed task that can spend real money. Follow-up phases
will extend coverage to other endpoints, WebSockets, and a Redis-backed store
suitable for multi-instance deployments. See issue
[#796](https://github.com/D-sorganization/Maxwell_Daemon/issues/796) for the
roadmap.

## What's enforced today

| Endpoint           | Default policy                | Enabled by default |
|--------------------|-------------------------------|--------------------|
| `POST /api/dispatch` | 10 requests / 60s per client | No                 |

The limiter uses a sliding-window algorithm keyed by client identity. It is
**disabled by default** so that upgrading to a release that includes this
module is a no-op until you opt in.

## Client identity

The limiter buckets requests by the first match in this list:

1. `request.state.jwt_sub` — the subject claim of a verified JWT, when JWT
   auth is configured (see `api.jwt_secret`).
2. A SHA-256 prefix of the bearer token (only when no JWT subject is
   available) — keeps the key space bounded without ever logging the raw
   credential.
3. The left-most entry of `X-Forwarded-For` — useful when the daemon sits
   behind a trusted reverse proxy that terminates TLS.
4. `request.client.host` — the direct connection IP.

If none of the above resolves, the request is bucketed under the literal
`ip:unknown` key.

## Response headers

Every request that passes through the limiter (whether allowed or rejected)
carries the standard [draft-ietf-httpapi-ratelimit-headers] fields:

```
RateLimit-Limit: 10
RateLimit-Remaining: 7
RateLimit-Reset: 42
```

* `RateLimit-Limit` — total requests permitted in the current window.
* `RateLimit-Remaining` — requests left before throttling kicks in.
* `RateLimit-Reset` — seconds until the oldest in-window request expires.

When the limit is exceeded the daemon returns `HTTP 429 Too Many Requests`
with a `Retry-After` header (also in seconds) and a JSON body:

```json
{
  "detail": "Rate limit exceeded for dispatch. Retry after 42 seconds."
}
```

## Configuration

Add the following block under `api:` in `~/.config/maxwell-daemon/config.toml`
(or the YAML equivalent):

```yaml
api:
  dispatch_rate_limit:
    enabled: true
    limit: 10            # max requests per window per client
    window_seconds: 60   # rolling window length
```

Restart the daemon to pick up the change. The limiter only takes effect once
`enabled: true` is set; default-constructed configs leave it off.

## Observability

The daemon publishes a Prometheus counter for every rejection:

```
rate_limit_exceeded_total{endpoint="dispatch"}
```

A reasonable starting alert is:

```
sum(rate(rate_limit_exceeded_total{endpoint="dispatch"}[5m])) > 0.1
```

i.e. any sustained throttling on the dispatch endpoint should page the
operator — either the limit is too tight or a client is misbehaving.

## Out of scope (for phase 1)

The following are explicitly deferred to follow-up issues:

* Limits on read-only endpoints (`/api/status`, `/api/v1/tasks`, …).
* WebSocket connection / event-rate caps.
* A Redis-backed `RateLimitStore` for multi-instance deployments.
* A richer per-user identity extractor that pulls the verified JWT subject
  through every middleware layer.

[draft-ietf-httpapi-ratelimit-headers]: https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/
