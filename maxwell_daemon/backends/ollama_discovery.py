"""Ollama local-model discovery.

Queries the Ollama REST API (``GET /api/tags``) to enumerate models that are
available on the local machine.  The result is a typed ``OllamaModelInfo``
list so callers can inspect name, size, and family without parsing raw dicts.

Usage
-----
Synchronous (e.g. CLI, startup checks)::

    from maxwell_daemon.backends.ollama_discovery import discover_ollama_models

    models = discover_ollama_models()          # uses http://localhost:11434
    for m in models:
        print(m.name, m.size_gb, m.family)

Async (inside an agent loop or FastAPI handler)::

    models = await adiscover_ollama_models()

Ollama not running
------------------
Both functions return an empty list when the Ollama server is unreachable
or returns a non-200 status, rather than raising an exception.  Use
``is_ollama_available()`` / ``ais_ollama_available()`` to probe liveness
without fetching the full model list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx

from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "OllamaModelInfo",
    "adiscover_ollama_models",
    "ais_ollama_available",
    "discover_ollama_models",
    "is_ollama_available",
]

_DEFAULT_ENDPOINT = "http://localhost:11434"
_TIMEOUT = 5.0


def _endpoint() -> str:
    """Return the Ollama base URL, honouring ``OLLAMA_HOST`` if set."""
    raw = os.environ.get("OLLAMA_HOST", _DEFAULT_ENDPOINT).strip().rstrip("/")
    if raw and "://" not in raw:
        raw = f"http://{raw}"
    return raw or _DEFAULT_ENDPOINT


@dataclass(slots=True, frozen=True)
class OllamaModelInfo:
    """Parsed information about a single Ollama model.

    Attributes
    ----------
    name:
        Full model tag as reported by Ollama (e.g. ``"llama3.1:8b"``).
    size_bytes:
        Model size in bytes (0 when not reported by the server).
    size_gb:
        Convenience property: size in gigabytes rounded to two decimal places.
    family:
        Model family extracted from the name prefix (e.g. ``"llama3.1"``).
    digest:
        Content digest string from the Ollama API (empty string if absent).
    details:
        Raw ``details`` dict from the API response for further inspection.
    """

    name: str
    size_bytes: int = 0
    digest: str = ""
    details: dict[str, object] = field(default_factory=dict)

    @property
    def size_gb(self) -> float:
        """Model size in gigabytes, rounded to two decimal places."""
        return round(self.size_bytes / 1_073_741_824, 2)

    @property
    def family(self) -> str:
        """Model family: the part of the name before the first colon."""
        return self.name.split(":")[0]


def _parse_models(data: dict[str, object]) -> list[OllamaModelInfo]:
    """Convert the raw ``/api/tags`` response into typed ``OllamaModelInfo`` objects."""
    raw_models = data.get("models")
    if not isinstance(raw_models, list):
        return []
    result: list[OllamaModelInfo] = []
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model") or ""
        if not name:
            continue
        result.append(
            OllamaModelInfo(
                name=str(name),
                size_bytes=int(entry.get("size", 0)),
                digest=str(entry.get("digest", "")),
                details=dict(entry.get("details") or {}),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Synchronous API
# ---------------------------------------------------------------------------


def discover_ollama_models(endpoint: str | None = None) -> list[OllamaModelInfo]:
    """Return all models currently available in the local Ollama installation.

    Parameters
    ----------
    endpoint:
        Ollama base URL.  Defaults to ``$OLLAMA_HOST`` or
        ``http://localhost:11434``.

    Returns
    -------
    list[OllamaModelInfo]
        Empty list when Ollama is unreachable or has no models.
    """
    base = (endpoint or _endpoint()).rstrip("/")
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(f"{base}/api/tags")
            resp.raise_for_status()
            return _parse_models(resp.json())
    except Exception as exc:
        log.debug("ollama_discovery: could not reach %s: %s", base, exc)
        return []


def is_ollama_available(endpoint: str | None = None) -> bool:
    """Return ``True`` when the Ollama server is reachable and healthy."""
    base = (endpoint or _endpoint()).rstrip("/")
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(f"{base}/api/tags")
            return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Async API
# ---------------------------------------------------------------------------


async def adiscover_ollama_models(endpoint: str | None = None) -> list[OllamaModelInfo]:
    """Async version of :func:`discover_ollama_models`.

    Parameters
    ----------
    endpoint:
        Ollama base URL.  Defaults to ``$OLLAMA_HOST`` or
        ``http://localhost:11434``.
    """
    base = (endpoint or _endpoint()).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{base}/api/tags")
            resp.raise_for_status()
            return _parse_models(resp.json())
    except Exception as exc:
        log.debug("ollama_discovery: could not reach %s: %s", base, exc)
        return []


async def ais_ollama_available(endpoint: str | None = None) -> bool:
    """Async version of :func:`is_ollama_available`."""
    base = (endpoint or _endpoint()).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{base}/api/tags")
            return resp.status_code == 200
    except Exception:
        return False


if __name__ == "__main__":
    # Quick CLI smoke test: python -m maxwell_daemon.backends.ollama_discovery
    models = discover_ollama_models()
    if not models:
        print("No Ollama models found (is Ollama running?)")
    else:
        print(f"Found {len(models)} Ollama model(s):")
        for m in models:
            print(f"  {m.name:<40} {m.size_gb:>6.2f} GB  family={m.family}")
