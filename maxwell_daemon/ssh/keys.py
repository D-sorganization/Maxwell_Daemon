"""SSH key management — load, generate, and store per-machine keys.

Keys are stored as PEM files under a configurable directory (default
``~/.maxwell-daemon/ssh-keys/``).  The directory is created with mode 0700
on first use.

Usage::

    store = SSHKeyStore(Path("~/.maxwell-daemon/ssh-keys"))
    priv, pub = store.get_or_generate("my-server")
    # priv is an asyncssh private key object; pub is the OpenSSH public key string.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

__all__ = ["SSHKeyStore"]


def _require_asyncssh() -> Any:
    try:
        import asyncssh

        return asyncssh
    except ModuleNotFoundError as exc:
        raise ImportError(
            "asyncssh is required for SSH features — install with: pip install maxwell-daemon[ssh]"
        ) from exc


class SSHKeyStore:
    """Per-machine SSH key store backed by PEM files on disk.

    Parameters
    ----------
    root:
        Directory where key files are stored.  Created with mode 0700 if absent.
    key_type:
        Algorithm for generated keys.  Defaults to ed25519 (fast, secure).
    key_bits:
        Key size for RSA keys (ignored for ed25519/ecdsa).
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        key_type: str = "ssh-ed25519",
        key_bits: int = 4096,
    ) -> None:
        self._root = (root or Path.home() / ".maxwell-daemon" / "ssh-keys").expanduser()
        self._key_type = key_type
        self._key_bits = key_bits

    def _ensure_dir(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._root.chmod(0o700)

    def _priv_path(self, name: str) -> Path:
        return self._root / f"{name}.pem"

    def _pub_path(self, name: str) -> Path:
        return self._root / f"{name}.pub"

    def get_or_generate(self, name: str) -> tuple[Any, str]:
        """Return (private_key, public_key_string) for *name*, generating if absent."""
        asyncssh = _require_asyncssh()
        priv_path = self._priv_path(name)
        if priv_path.is_file():
            priv = asyncssh.read_private_key(str(priv_path))
            pub_path = self._pub_path(name)
            if pub_path.is_file():
                pub_str = pub_path.read_text(encoding="utf-8").strip()
            else:
                pub_str = priv.export_public_key().decode().strip()
            return priv, pub_str

        self._ensure_dir()
        priv = asyncssh.generate_private_key(
            self._key_type,
            comment=f"maxwell-daemon/{name}",
            **(
                {"key_size": self._key_bits}
                if self._key_type in ("rsa", "ssh-rsa")
                else {}
            ),
        )
        priv_path.write_bytes(priv.export_private_key())
        priv_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
        pub_str = priv.export_public_key().decode().strip()
        self._pub_path(name).write_text(pub_str + "\n", encoding="utf-8")
        return priv, pub_str

    def public_key_string(self, name: str) -> str | None:
        """Return the OpenSSH public key for *name*, or None if not found."""
        pub_path = self._pub_path(name)
        return (
            pub_path.read_text(encoding="utf-8").strip() if pub_path.is_file() else None
        )

    def list_machines(self) -> list[str]:
        """Return the names of all machines that have stored keys."""
        if not self._root.is_dir():
            return []
        return sorted(p.stem for p in self._root.glob("*.pem"))

    def remove(self, name: str) -> None:
        """Delete both key files for *name*.  No-op if they don't exist."""
        self._priv_path(name).unlink(missing_ok=True)
        self._pub_path(name).unlink(missing_ok=True)
