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
    # Where the collector clones targets from, and how long one clone may take.
    github_base_url: str = "https://github.com"
    clone_timeout_s: float = 300.0
    # Magickit's MCP endpoint (Streamable HTTP, e.g. http://host:8114/mcp), the
    # source of the service_health facts. No default: an unset URL makes that
    # extractor fail loudly (run ``partial``) rather than probe a guessed host.
    magickit_url: str | None = None
    magickit_timeout_s: float = 60.0


def get_settings() -> Settings:
    """Build settings from the current environment.

    Deliberately not cached: tests and the CLI both mutate the environment.
    """
    return Settings()
