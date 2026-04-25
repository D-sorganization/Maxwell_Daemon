"""OS-backed secret store helpers."""

from __future__ import annotations

from typing import Any, Protocol, cast

DEFAULT_SERVICE_NAME = "maxwell-daemon"


class SecretStore(Protocol):
    """Minimal interface used by config migration/resolution."""

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


def backend_api_key_secret_ref(backend_name: str) -> str:
    """Canonical secret ref for a backend API key."""
    return f"{DEFAULT_SERVICE_NAME}/backends/{backend_name}/api_key"


class KeyringSecretStore:
    """Thin wrapper around the keyring package."""

    def __init__(
        self, service_name: str = DEFAULT_SERVICE_NAME, keyring_module: Any = None
    ) -> None:
        self._service_name = service_name
        if keyring_module is None:
            try:
                import keyring as imported_keyring
            except (
                ImportError
            ) as exc:  # pragma: no cover - exercised via dependency installs
                raise RuntimeError(
                    "keyring is required for OS-backed secret storage. "
                    "Install maxwell-daemon with its keyring dependency."
                ) from exc
            keyring_module = imported_keyring
        self._keyring = keyring_module

    def get(self, name: str) -> str | None:
        return cast(str | None, self._keyring.get_password(self._service_name, name))

    def set(self, name: str, value: str) -> None:
        self._keyring.set_password(self._service_name, name, value)

    def delete(self, name: str) -> None:
        self._keyring.delete_password(self._service_name, name)
