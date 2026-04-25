from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from maxwell_daemon.ssh import keys as ssh_keys
from maxwell_daemon.ssh import session as ssh_session


class _FakePrivateKey:
    def __init__(self, public_key: str = "ssh-ed25519 fake-public") -> None:
        self.public_key = public_key

    def export_private_key(self) -> bytes:
        return b"fake-private-key"

    def export_public_key(self) -> bytes:
        return self.public_key.encode()


class _FakeAsyncSSH:
    def __init__(self) -> None:
        self.generated: list[tuple[str, dict[str, Any]]] = []
        self.connected_with: list[dict[str, Any]] = []

    def generate_private_key(self, key_type: str, **kwargs: Any) -> _FakePrivateKey:
        self.generated.append((key_type, kwargs))
        return _FakePrivateKey()

    def read_private_key(self, path: str) -> _FakePrivateKey:
        return _FakePrivateKey(f"ssh-ed25519 loaded-from-{Path(path).stem}")

    async def connect(self, **kwargs: Any) -> MagicMock:
        self.connected_with.append(kwargs)
        conn = MagicMock()
        conn.close = MagicMock()
        conn.wait_closed = AsyncMock()
        return conn


def test_key_store_generates_and_reuses_key_without_optional_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_asyncssh = _FakeAsyncSSH()
    monkeypatch.setattr(ssh_keys, "_require_asyncssh", lambda: fake_asyncssh)

    store = ssh_keys.SSHKeyStore(tmp_path)
    _private_key, public_key = store.get_or_generate("workstation")

    assert public_key == "ssh-ed25519 fake-public"
    assert (tmp_path / "workstation.pem").read_bytes() == b"fake-private-key"
    assert (tmp_path / "workstation.pub").read_text(
        encoding="utf-8"
    ).strip() == public_key
    assert fake_asyncssh.generated == [
        ("ssh-ed25519", {"comment": "maxwell-daemon/workstation"})
    ]

    _loaded_key, loaded_public_key = store.get_or_generate("workstation")
    assert loaded_public_key == public_key


def test_key_store_derives_public_key_when_sidecar_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_asyncssh = _FakeAsyncSSH()
    monkeypatch.setattr(ssh_keys, "_require_asyncssh", lambda: fake_asyncssh)
    (tmp_path / "server.pem").write_bytes(b"existing-key")

    store = ssh_keys.SSHKeyStore(tmp_path)

    _loaded_key, public_key = store.get_or_generate("server")

    assert public_key == "ssh-ed25519 loaded-from-server"


def test_require_asyncssh_reports_install_extra_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(__import__("sys").modules, "asyncssh", None)

    with pytest.raises(ImportError, match=r"pip install maxwell-daemon\[ssh\]"):
        ssh_keys._require_asyncssh()


@pytest.mark.asyncio
async def test_session_pool_uses_password_without_generating_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_asyncssh = _FakeAsyncSSH()
    key_store = MagicMock(spec=ssh_keys.SSHKeyStore)
    pool = ssh_session.SSHSessionPool(key_store, known_hosts=tmp_path / "known_hosts")
    monkeypatch.setattr(ssh_session, "_require_asyncssh", lambda: fake_asyncssh)

    session = await pool.get(
        "desktop.tailnet.example",
        user="dieter",
        port=2022,
        password="secret",
    )

    assert isinstance(session, ssh_session.SSHSession)
    assert fake_asyncssh.connected_with == [
        {
            "host": "desktop.tailnet.example",
            "port": 2022,
            "username": "dieter",
            "known_hosts": str(tmp_path / "known_hosts"),
            "password": "secret",
        }
    ]
    key_store.get_or_generate.assert_not_called()


@pytest.mark.asyncio
async def test_session_pool_closes_replaced_expired_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_asyncssh = _FakeAsyncSSH()
    pool = ssh_session.SSHSessionPool(known_hosts=None)
    monkeypatch.setattr(ssh_session, "_require_asyncssh", lambda: fake_asyncssh)
    monkeypatch.setattr(
        pool._key_store,
        "get_or_generate",
        lambda host: (_FakePrivateKey(f"ssh-ed25519 {host}"), "unused"),
    )

    first = await pool.get("worker", user="maxwell")
    first.created_at -= ssh_session.SESSION_TTL_SECONDS + 1
    second = await pool.get("worker", user="maxwell")

    assert first is not second
    first._conn.close.assert_called_once()
    first._conn.wait_closed.assert_awaited_once()
    assert pool.sessions()[0]["host"] == "worker"
