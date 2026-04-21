"""GitHub webhook receiver — signature verification, routing, rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from maxwell_daemon.gh.webhook import (
    WebhookConfig,
    WebhookDispatch,
    WebhookRoute,
    WebhookRouter,
    verify_signature,
)


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class TestVerifySignature:
    def test_valid_signature_accepted(self) -> None:
        body = b'{"zen":"hello"}'
        sig = _sign("secret", body)
        assert verify_signature("secret", body, sig) is True

    def test_wrong_signature_rejected(self) -> None:
        body = b'{"zen":"hello"}'
        sig = _sign("other-secret", body)
        assert verify_signature("secret", body, sig) is False

    def test_missing_prefix_rejected(self) -> None:
        body = b"{}"
        digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        assert verify_signature("secret", body, digest) is False  # no sha256= prefix

    def test_empty_signature_rejected(self) -> None:
        assert verify_signature("secret", b"{}", "") is False

    def test_tampered_body_rejected(self) -> None:
        body = b'{"zen":"hello"}'
        sig = _sign("secret", body)
        assert verify_signature("secret", b'{"zen":"goodbye"}', sig) is False


class _FakeDaemon:
    def __init__(self) -> None:
        self.issue_calls: list[dict[str, Any]] = []

    def submit_issue(
        self,
        *,
        repo: str,
        issue_number: int,
        mode: str = "plan",
        backend: str | None = None,
        model: str | None = None,
    ) -> Any:
        self.issue_calls.append(
            {"repo": repo, "issue_number": issue_number, "mode": mode}
        )

        class _Task:
            id = f"task-{len(self.issue_calls)}"

        return _Task()


@pytest.fixture
def config() -> WebhookConfig:
    return WebhookConfig(
        secret="secret",
        allowed_repos=["owner/allowed"],
        routes=[
            WebhookRoute(
                event="issues",
                action="opened",
                label="maxwell-daemon-plan",
                mode="plan",
            ),
            WebhookRoute(
                event="issues",
                action="opened",
                label="maxwell-daemon-implement",
                mode="implement",
            ),
            WebhookRoute(
                event="issue_comment",
                action="created",
                trigger="/maxwell-daemon plan",
                mode="plan",
            ),
        ],
    )


def _issue_payload(
    repo: str, number: int, labels: list[str], action: str = "opened"
) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": repo},
        "issue": {
            "number": number,
            "title": "t",
            "body": "b",
            "labels": [{"name": n} for n in labels],
        },
    }


def _comment_payload(
    repo: str, number: int, body: str, action: str = "created"
) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": repo},
        "issue": {"number": number},
        "comment": {"body": body},
    }


class TestWebhookRouter:
    def test_issue_opened_with_matching_label_dispatches(
        self, config: WebhookConfig
    ) -> None:
        daemon = _FakeDaemon()
        router = WebhookRouter(config, daemon=daemon)
        dispatches = router.handle(
            event_type="issues",
            payload=_issue_payload("owner/allowed", 7, ["bug", "maxwell-daemon-plan"]),
        )
        assert len(dispatches) == 1
        assert dispatches[0].mode == "plan"
        assert daemon.issue_calls == [
            {"repo": "owner/allowed", "issue_number": 7, "mode": "plan"}
        ]

    def test_issue_opened_without_matching_label_is_noop(
        self, config: WebhookConfig
    ) -> None:
        daemon = _FakeDaemon()
        router = WebhookRouter(config, daemon=daemon)
        dispatches = router.handle(
            event_type="issues",
            payload=_issue_payload("owner/allowed", 7, ["bug"]),
        )
        assert dispatches == []
        assert daemon.issue_calls == []

    def test_disallowed_repo_is_refused(self, config: WebhookConfig) -> None:
        daemon = _FakeDaemon()
        router = WebhookRouter(config, daemon=daemon)
        dispatches = router.handle(
            event_type="issues",
            payload=_issue_payload("someone/else", 7, ["maxwell-daemon-plan"]),
        )
        assert dispatches == []
        assert daemon.issue_calls == []

    def test_implement_label_dispatches_implement_mode(
        self, config: WebhookConfig
    ) -> None:
        daemon = _FakeDaemon()
        router = WebhookRouter(config, daemon=daemon)
        router.handle(
            event_type="issues",
            payload=_issue_payload("owner/allowed", 9, ["maxwell-daemon-implement"]),
        )
        assert daemon.issue_calls == [
            {"repo": "owner/allowed", "issue_number": 9, "mode": "implement"}
        ]

    def test_comment_trigger_dispatches(self, config: WebhookConfig) -> None:
        daemon = _FakeDaemon()
        router = WebhookRouter(config, daemon=daemon)
        router.handle(
            event_type="issue_comment",
            payload=_comment_payload(
                "owner/allowed", 42, "Please look at this. /maxwell-daemon plan"
            ),
        )
        assert daemon.issue_calls == [
            {"repo": "owner/allowed", "issue_number": 42, "mode": "plan"}
        ]

    def test_closed_issue_not_dispatched(self, config: WebhookConfig) -> None:
        """An `issues.closed` event should not match the `opened` route."""
        daemon = _FakeDaemon()
        router = WebhookRouter(config, daemon=daemon)
        dispatches = router.handle(
            event_type="issues",
            payload=_issue_payload(
                "owner/allowed", 1, ["maxwell-daemon-plan"], action="closed"
            ),
        )
        assert dispatches == []

    def test_ping_event_ignored(self, config: WebhookConfig) -> None:
        daemon = _FakeDaemon()
        router = WebhookRouter(config, daemon=daemon)
        dispatches = router.handle(event_type="ping", payload={"zen": "hi"})
        assert dispatches == []


class TestWebhookEndpoint:
    """Black-box FastAPI test — exercises the real endpoint + signature checks."""

    def _setup(self, secret: str = "topsecret") -> tuple[Any, _FakeDaemon]:
        from fastapi.testclient import TestClient

        from maxwell_daemon.api import create_app
        from maxwell_daemon.config import MaxwellDaemonConfig
        from maxwell_daemon.daemon import Daemon

        cfg = MaxwellDaemonConfig.model_validate(
            {
                "backends": {"x": {"type": "ollama", "model": "y"}},
                "agent": {"default_backend": "x"},
                "github": {
                    "webhook_secret": secret,
                    "allowed_repos": ["owner/allowed"],
                    "routes": [
                        {
                            "event": "issues",
                            "action": "opened",
                            "label": "maxwell-daemon-plan",
                            "mode": "plan",
                        }
                    ],
                },
            }
        )
        daemon = Daemon(cfg, ledger_path=None)
        client = TestClient(create_app(daemon))
        return client, daemon

    def test_valid_signature_accepted(self) -> None:
        client, daemon = self._setup()
        body = json.dumps(
            _issue_payload("owner/allowed", 1, ["maxwell-daemon-plan"])
        ).encode()
        sig = _sign("topsecret", body)
        r = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "x-hub-signature-256": sig,
                "x-github-event": "issues",
                "content-type": "application/json",
            },
        )
        assert r.status_code == 200
        # Since we used a real daemon (not a fake), the task is queued but we
        # can still observe it through state().
        tasks = daemon.state().tasks
        assert any(t.kind.value == "issue" for t in tasks.values())

    def test_missing_signature_rejected(self) -> None:
        client, _ = self._setup()
        r = client.post(
            "/api/v1/webhooks/github",
            content=b"{}",
            headers={"x-github-event": "issues", "content-type": "application/json"},
        )
        assert r.status_code == 401

    def test_bad_signature_rejected(self) -> None:
        client, _ = self._setup()
        r = client.post(
            "/api/v1/webhooks/github",
            content=b"{}",
            headers={
                "x-hub-signature-256": "sha256=deadbeef",
                "x-github-event": "issues",
                "content-type": "application/json",
            },
        )
        assert r.status_code == 401

    def test_ping_returns_200(self) -> None:
        client, _ = self._setup()
        body = b'{"zen":"hello"}'
        sig = _sign("topsecret", body)
        r = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "x-hub-signature-256": sig,
                "x-github-event": "ping",
                "content-type": "application/json",
            },
        )
        assert r.status_code == 200

    def test_malformed_json_rejected(self) -> None:
        client, _ = self._setup()
        body = b"not json"
        sig = _sign("topsecret", body)
        r = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "x-hub-signature-256": sig,
                "x-github-event": "issues",
                "content-type": "application/json",
            },
        )
        assert r.status_code == 400


class TestWebhookDispatch:
    def test_dataclass_fields(self) -> None:
        d = WebhookDispatch(task_id="t-1", repo="o/r", issue_number=5, mode="plan")
        assert d.task_id == "t-1"
        assert d.repo == "o/r"
