"""Runtime settings for the Verimend service.

Values come from the environment with the ``VERIMEND_`` prefix, e.g.
``VERIMEND_PORT=9118``. Defaults match docs/design.md section 3 (port 8118).

Path defaults are relative to the working directory, so running the service
from a checkout picks up the committed ``config/targets.yaml``; a systemd unit
sets ``WorkingDirectory`` or passes absolute paths via the environment.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-level configuration."""

    model_config = SettingsConfigDict(env_prefix="VERIMEND_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8118
    db_path: Path = Path("var/verimend.sqlite3")
    targets_path: Path = Path("config/targets.yaml")


def get_settings() -> Settings:
    """Build settings from the current environment.

    Deliberately not cached: tests and the CLI both mutate the environment.
    """
    return Settings()
