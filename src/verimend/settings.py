"""Runtime settings for the Verimend service.

Values come from the environment with the ``VERIMEND_`` prefix, e.g.
``VERIMEND_PORT=9118``. Defaults match docs/design.md section 3 (port 8118).

Path defaults are relative to the working directory, so running the service
from a checkout picks up the committed ``config/targets.yaml``; a systemd unit
sets ``WorkingDirectory`` or passes absolute paths via the environment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
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
    # Lexora's OpenAI-compatible API (e.g. http://host:8110), used by the
    # extractor. No default: an unset URL fails the run (``failed``) before any
    # chunk is built, rather than producing a run with zero claims.
    lexora_url: str | None = None
    # The httpx connect/read timeout of one Lexora request, and nothing else.
    lexora_timeout_s: float = 120.0
    # The longest Retry-After (429 / 503) the extractor will wait out. A longer
    # one aborts the extraction stage at once. Independent of lexora_timeout_s.
    lexora_retry_after_max_s: float = 120.0
    # The Lexora tier the extractor asks for (docs/design.md 6).
    extractor_tier: str = "light"
    # The Lexora tier the reconciler's LLM judge asks for (docs/design.md 6).
    # An empty value is rejected here; a tier Lexora does not know is caught at
    # the first request (404: configuration error; repeated 4xx: an abort).
    reconciler_tier: str = "light"
    # The Lexora tier the mender asks for its patch drafts (docs/design.md 6:
    # Heavy -- few calls, quality first). Empty is rejected like reconciler_tier.
    mender_tier: str = "heavy"
    # What the mender does with its drafts (docs/design.md 5.4). ``dry_run`` makes
    # the local branch and commit and writes the PR body under mend_artefact_dir;
    # only ``github`` pushes and opens a pull request.
    mend_publish: Literal["dry_run", "github"] = "dry_run"
    mend_artefact_dir: Path = Path("var/mend")
    # The author of the commits the mender makes. Empty is rejected.
    git_author_name: str = "verimend"
    git_author_email: str = "verimend@spirrow.invalid"
    # The GitHub CLI the mender runs in ``github`` mode.
    gh_bin: str = "gh"
    # The Lexora /v1/decide judgements around the mender (T06, verimend.decide).
    # ``off`` never calls Lexora; ``observe`` calls and records, applying nothing;
    # ``enforce`` applies the skip / routing rules. Enabling anything but ``off``
    # in production is a separate cost decision, not made by the code default.
    decide_mode: Literal["off", "observe", "enforce"] = "off"
    decide_drift_min_level: int = 1
    decide_min_mapping_correct: float = 0.40
    decide_auto_pr_max_risk: float = 0.25
    decide_min_confidence: float = 0.60
    decide_findings_only_min_claim_risk: float = 0.75

    @field_validator("decide_drift_min_level")
    @classmethod
    def _level_in_range(cls, value: int) -> int:
        if not 0 <= value <= 3:
            raise ValueError("decide_drift_min_level must be within 0..3")
        return value

    @field_validator(
        "decide_min_mapping_correct",
        "decide_auto_pr_max_risk",
        "decide_min_confidence",
        "decide_findings_only_min_claim_risk",
    )
    @classmethod
    def _probability(cls, value: float, info) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{info.field_name} must be within 0..1")
        return value

    @model_validator(mode="after")
    def _decide_consistent(self) -> "Settings":
        if not self.decide_auto_pr_max_risk < self.decide_findings_only_min_claim_risk:
            raise ValueError("decide_auto_pr_max_risk must be below decide_findings_only_min_claim_risk")
        unknown = unknown_decide_keys(os.environ)
        if unknown:
            raise ValueError(f"unknown {DECIDE_ENV_PREFIX}* settings: {', '.join(unknown)}")
        return self

    @field_validator("reconciler_tier", "mender_tier", "git_author_name", "git_author_email")
    @classmethod
    def _tier_not_empty(cls, value: str, info) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return value


DECIDE_ENV_PREFIX = "VERIMEND_DECIDE_"


def unknown_decide_keys(environ) -> list[str]:
    """``VERIMEND_DECIDE_*`` variables that name no field: a typo must not silently leave the default.

    ``extra="ignore"`` keeps the rest of the namespace lenient; only the decide
    namespace is closed (T06: unknown decide keys are rejected).
    """
    known = {f"VERIMEND_{name.upper()}" for name in Settings.model_fields if name.startswith("decide_")}
    return sorted(k for k in environ if k.upper().startswith(DECIDE_ENV_PREFIX) and k.upper() not in known)


def get_settings() -> Settings:
    """Build settings from the current environment.

    Deliberately not cached: tests and the CLI both mutate the environment.
    """
    return Settings()
