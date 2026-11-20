"""Installed distribution version.

``pyproject.toml`` is the only version source.  Keeping runtime code free of a
second literal prevents release automation from producing mismatched versions.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("meldfabric")
except PackageNotFoundError:  # pragma: no cover - only direct, uninstalled source imports
    __version__ = "0+unknown"
