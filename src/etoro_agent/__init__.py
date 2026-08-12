"""eToro DEMO/paper trading agent with a deterministic risk boundary."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("etoro-demo-agent")
except PackageNotFoundError:  # pragma: no cover - only an unpackaged source tree
    __version__ = "0+unknown"
