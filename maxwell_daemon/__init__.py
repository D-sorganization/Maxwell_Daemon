"""Maxwell-Daemon — Multi-backend autonomous code agent orchestrator."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("maxwell-daemon")
except PackageNotFoundError:  # pragma: no cover — only hit in uninstalled/dev checkouts
    __version__ = "unknown"

__all__ = ["__version__"]
