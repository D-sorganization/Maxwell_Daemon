"""Modular HTTP route registrations for the Maxwell-Daemon API.

Each submodule exposes a ``register(app, daemon, ...)`` callable that
attaches a cohesive group of endpoints to the FastAPI app.  This package
exists to incrementally decompose ``maxwell_daemon/api/server.py`` (see
issue #793) without breaking the append-only HTTP contract.
"""

from __future__ import annotations
