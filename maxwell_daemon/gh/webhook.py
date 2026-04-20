"""GitHub webhook receiver.

Handles signature verification, event parsing, and dispatch routing. Kept
orthogonal to the FastAPI endpoint — this module is pure logic so it's easy
to test and to reuse for other transports (e.g. a relay from a third-party
proxy that already terminated TLS).

Security model
--------------
GitHub webhooks are authenticated via an HMAC-SHA256 signature over the raw
request body, carried in the ``X-Hub-Signature-256`` header. Any endpoint
that skips signature verification is an attacker's RCE — we use
``hmac.compare_digest`` throughout to prevent timing-based token recovery.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

__all__ = [
    "WebhookConfig",
    "WebhookDispatch",
    "WebhookRoute",
    "WebhookRouter",
    "verify_signature",
]


@dataclass(slots=True, frozen=True)
class WebhookRoute:
    """One dispatch rule in the webhook config."""

    event: str  # "issues", "issue_comment", ...
    action: str  # "opened", "closed", "created", ...
    mode: Literal["plan", "implement"] = "plan"
    label: str | None = None  # if set, issue must carry this label
    trigger: str | None = None  # if set, comment body must contain this substring

    def matches(self, *, event_type: str, action: str) -> bool:
        return event_type == self.event and action == self.action


@dataclass(slots=True)
class WebhookConfig:
    secret: str
    allowed_repos: list[str] = field(default_factory=list)
    routes: list[WebhookRoute] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class WebhookDispatch:
    task_id: str
    repo: str
    issue_number: int
    mode: Literal["plan", "implement"]


class _DaemonProto(Protocol):
    def submit_issue(
        self,
        *,
        repo: str,
        issue_number: int,
        mode: str = "plan",
        backend: str | None = None,
        model: str | None = None,
    ) -> Any: ...


def verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Constant-time verify of a GitHub ``X-Hub-Signature-256`` header.

    Returns False for any malformed input rather than raising, so callers can
    uniformly return 401 on any auth failure.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    presented = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, presented)


class WebhookRouter:
    """Translates verified webhook events into daemon dispatches."""

    def __init__(self, config: WebhookConfig, *, daemon: _DaemonProto) -> None:
        self._config = config
        self._daemon = daemon

    def handle(
        self, *, event_type: str, payload: dict[str, Any]
    ) -> list[WebhookDispatch]:
        if event_type == "ping":
            return []

        action = str(payload.get("action", ""))
        repo = str(payload.get("repository", {}).get("full_name", ""))

        if not repo or repo not in self._config.allowed_repos:
            return []

        matching_routes = [
            r
            for r in self._config.routes
            if r.matches(event_type=event_type, action=action)
        ]
        if not matching_routes:
            return []

        if event_type == "issues":
            return self._dispatch_issues(payload, repo, matching_routes)
        if event_type == "issue_comment":
            return self._dispatch_comments(payload, repo, matching_routes)
        return []

    def _dispatch_issues(
        self, payload: dict[str, Any], repo: str, routes: list[WebhookRoute]
    ) -> list[WebhookDispatch]:
        issue = payload.get("issue", {})
        number = int(issue.get("number", 0))
        if number <= 0:
            return []
        labels = set()
        for label in issue.get("labels", []):
            if isinstance(label, dict) and "name" in label:
                labels.add(label["name"])
            elif isinstance(label, str):
                labels.add(label)

        out: list[WebhookDispatch] = []
        for route in routes:
            if route.label and route.label not in labels:
                continue
            task = self._daemon.submit_issue(
                repo=repo, issue_number=number, mode=route.mode
            )
            out.append(
                WebhookDispatch(
                    task_id=task.id,
                    repo=repo,
                    issue_number=number,
                    mode=route.mode,
                )
            )
        return out

    def _dispatch_comments(
        self, payload: dict[str, Any], repo: str, routes: list[WebhookRoute]
    ) -> list[WebhookDispatch]:
        comment_body = str(payload.get("comment", {}).get("body", ""))
        issue_number = int(payload.get("issue", {}).get("number", 0))
        if issue_number <= 0:
            return []

        out: list[WebhookDispatch] = []
        for route in routes:
            if not route.trigger or route.trigger not in comment_body:
                continue
            task = self._daemon.submit_issue(
                repo=repo, issue_number=issue_number, mode=route.mode
            )
            out.append(
                WebhookDispatch(
                    task_id=task.id,
                    repo=repo,
                    issue_number=issue_number,
                    mode=route.mode,
                )
            )
        return out
