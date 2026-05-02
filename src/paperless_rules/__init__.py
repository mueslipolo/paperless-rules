"""paperless-rules: rule-based document classification for paperless-ngx."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("paperless-rules")
except PackageNotFoundError:
    __version__ = "0.0.0+dev"
