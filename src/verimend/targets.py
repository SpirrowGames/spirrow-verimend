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
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$"

# A doc glob is matched against a shallow clone of the target repository, so it
# has to be repository-relative. Both flavours are consulted rather than the
# host-native ``pathlib.Path``, because each flavour is blind to the other's
# syntax and a host-native check is therefore only as strict as the machine it
# happens to run on:
#
#   - ``Path`` is ``PosixPath`` on the Linux crawler and CI runner. There,
#     ``Path("C:/docs").anchor`` is "" and ``Path("C:/docs").is_absolute()`` is
#     False, so a drive-lettered path would be accepted exactly where the
#     crawler actually runs.
#   - ``Path`` is ``WindowsPath`` on a developer's Windows box. There,
#     backslashes separate components, so a POSIX-only check misses
#     ``docs\..\..\etc`` -- which ``PosixPath`` reads as one filename.
#
# Consulting both flavours makes the verdict identical on every host.
_PATH_FLAVOURS = (PurePosixPath, PureWindowsPath)


class ExtractorName(str, Enum):
    """Deterministic fact extractors (docs/design.md section 5.1)."""

    MCP_SCHEMA = "mcp_schema"
    CONFIG_KEYS = "config_keys"
    ENTRYPOINTS = "entrypoints"
    PORTS = "ports"
    SERVICE_HEALTH = "service_health"


class TargetsConfigError(ValueError):
    """Raised when ``targets.yaml`` is missing, unreadable, or invalid."""


def _glob_must_stay_in_repository(glob: str) -> None:
    """Raise unless ``glob`` can only ever match inside the repository clone.

    The test is pathlib's parse of the string under both flavours, not string
    prefix matching. A leading slash or backslash is only one of several ways to
    anchor a path, and the ways it misses -- drive letters and UNC shares -- are
    exactly the ones a prefix check lets through silently. See ``_PATH_FLAVOURS``
    for why both flavours are consulted instead of the host-native ``Path``.

    ``anchor`` is the predicate rather than ``is_absolute()`` because a
    drive-relative path such as ``"C:docs"`` is *not* absolute -- it is relative
    to whatever the current directory on drive C: happens to be -- yet it is
    still resolved outside the clone. ``anchor`` is non-empty for all three of
    a root, a drive, and a UNC share.

    One deliberate false positive follows: a repository-root file whose name
    contains a colon (``"a:b.md"``) parses as drive-relative under the Windows
    flavour and is refused. Such a name cannot be checked out on Windows at all,
    and this module fails loudly on ambiguity by design, so the trade is taken
    knowingly rather than papered over.
    """
    for flavour in _PATH_FLAVOURS:
        path = flavour(glob)
        if path.anchor:
            raise ValueError(f"doc glob must be repository-relative, not anchored: {glob!r}")
        if ".." in path.parts:
            raise ValueError(f"doc glob must stay inside the repository: {glob!r}")


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
            _glob_must_stay_in_repository(glob)
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
