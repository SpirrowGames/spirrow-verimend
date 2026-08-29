"""Loading and validation of ``config/targets.yaml``.

docs/design.md section 5.1: the crawl targets are declared in
``config/targets.yaml`` -- the repository, the documentation globs, and which
deterministic fact extractors are enabled or disabled for it.

Validation is strict on purpose: an unknown key or an unknown extractor name is
a configuration mistake that must fail loudly at load time, not silently skip
work during a nightly run.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$"


class ExtractorName(str, Enum):
    """Deterministic fact extractors (docs/design.md section 5.1)."""

    MCP_SCHEMA = "mcp_schema"
    CONFIG_KEYS = "config_keys"
    ENTRYPOINTS = "entrypoints"
    PORTS = "ports"
    SERVICE_HEALTH = "service_health"


class TargetsConfigError(ValueError):
    """Raised when ``targets.yaml`` is missing, unreadable, or invalid."""


class Target(BaseModel):
    """One crawl target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo: str = Field(pattern=REPO_PATTERN, description="GitHub repository as owner/name.")
    doc_globs: list[str] = Field(min_length=1, description="Documentation globs, relative to the repository root.")
    extractors: dict[ExtractorName, bool] = Field(
        default_factory=dict,
        description="Fact extractor enable/disable flags. Omitted extractors are disabled.",
    )

    @field_validator("doc_globs")
    @classmethod
    def _globs_must_be_relative(cls, globs: list[str]) -> list[str]:
        for glob in globs:
            if not glob or glob.strip() != glob:
                raise ValueError(f"doc glob must be a non-empty trimmed string: {glob!r}")
            if glob.startswith("/") or glob.startswith("\\") or ".." in Path(glob).parts:
                raise ValueError(f"doc glob must stay inside the repository: {glob!r}")
        return globs

    @model_validator(mode="after")
    def _at_least_one_extractor(self) -> "Target":
        if not any(self.extractors.values()):
            raise ValueError(f"target {self.repo!r} has no enabled extractor")
        return self

    @property
    def enabled_extractors(self) -> tuple[ExtractorName, ...]:
        """Extractors enabled for this target, in declaration order."""
        return tuple(name for name, enabled in self.extractors.items() if enabled)


class TargetsConfig(BaseModel):
    """Top level of ``config/targets.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    targets: list[Target] = Field(min_length=1)

    @model_validator(mode="after")
    def _repos_are_unique(self) -> "TargetsConfig":
        seen: set[str] = set()
        for target in self.targets:
            if target.repo in seen:
                raise ValueError(f"duplicate target repo: {target.repo!r}")
            seen.add(target.repo)
        return self

    def get(self, repo: str) -> Target | None:
        """Return the target for ``repo``, or None when it is not declared."""
        return next((t for t in self.targets if t.repo == repo), None)


def parse_targets(raw: Any, *, source: str = "<memory>") -> TargetsConfig:
    """Validate an already-parsed YAML document."""
    if not isinstance(raw, dict):
        raise TargetsConfigError(f"{source}: expected a YAML mapping at the top level, got {type(raw).__name__}")
    try:
        return TargetsConfig.model_validate(raw)
    except ValidationError as exc:
        raise TargetsConfigError(f"{source}: invalid targets configuration\n{exc}") from exc


def load_targets(path: Path) -> TargetsConfig:
    """Read and validate ``targets.yaml`` at ``path``."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise TargetsConfigError(f"{path}: cannot read targets file: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TargetsConfigError(f"{path}: not valid YAML: {exc}") from exc
    return parse_targets(raw, source=str(path))
