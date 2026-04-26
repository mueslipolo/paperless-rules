"""Environment-driven configuration. See .env.example for the full var list."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    paperless_url: str = ""
    paperless_token: str = ""
    paperless_verify_ssl: bool = True
    paperless_ca_bundle: str = ""
    rules_dir: Path = field(default_factory=lambda: Path("./rules"))
    state_dir: Path = field(default_factory=lambda: Path("./state"))
    editor_enabled: bool = True
    editor_host: str = "0.0.0.0"
    editor_port: int = 8765
    runtime_mode: str = "disabled"  # post_consume | poller | disabled
    poll_interval_seconds: int = 60
    poll_filter: str = ""

    @property
    def httpx_verify(self) -> bool | str:
        """Value to pass as httpx's `verify=` kwarg.

        - CA bundle path wins if set (proper way for self-signed setups
          with a local CA the user has exported).
        - Otherwise the bool: True (system CAs) or False (skip verification —
          fine for trusted LAN, dangerous for public networks).
        """
        return self.paperless_ca_bundle or self.paperless_verify_ssl

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        """Build a Config from environment variables (defaults to os.environ)."""
        e = env if env is not None else os.environ
        return cls(
            paperless_url=e.get("PAPERLESS_URL", "").rstrip("/"),
            paperless_token=e.get("PAPERLESS_TOKEN", ""),
            paperless_verify_ssl=e.get("PAPERLESS_VERIFY_SSL", "true").lower() in ("true", "1", "yes", "on"),
            paperless_ca_bundle=e.get("PAPERLESS_CA_BUNDLE", ""),
            rules_dir=Path(e.get("RULES_DIR", "./rules")),
            state_dir=Path(e.get("STATE_DIR", "./state")),
            editor_enabled=e.get("EDITOR_ENABLED", "true").lower() in ("true", "1", "yes", "on"),
            editor_host=e.get("EDITOR_HOST", "0.0.0.0"),
            editor_port=int(e.get("EDITOR_PORT", "8765")),
            runtime_mode=e.get("RUNTIME_MODE", "disabled"),
            poll_interval_seconds=int(e.get("POLL_INTERVAL_SECONDS", "60")),
            poll_filter=e.get("POLL_FILTER", ""),
        )
