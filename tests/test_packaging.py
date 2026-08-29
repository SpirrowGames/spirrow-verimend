"""The version is one fact, so it gets exactly one home.

pyproject.toml declares ``dynamic = ["version"]`` and points hatchling at
src/verimend/__init__.py, which is where ``__version__`` lives and where
/health reads it from. These tests go red the moment someone re-adds a
static ``version`` to pyproject.toml - the drift that keeping two copies
invites.

They read pyproject.toml rather than asking the installed distribution for
its version, because ``uv sync`` does not rebuild an editable install when
only __init__.py changes (measured): right after a bump the installed
metadata legitimately lags the source, so a metadata-based test would be
red without a defect. The built wheel is never stale - hatchling reads
__init__.py at build time.
"""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_HOME = "src/verimend/__init__.py"


def _pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_pyproject_does_not_hardcode_the_version() -> None:
    project = _pyproject()["project"]
    assert "version" not in project, (
        f"pyproject.toml hardcodes the version again; it belongs only in {VERSION_HOME}"
    )
    assert "version" in project.get("dynamic", [])


def test_hatchling_reads_the_version_from_the_package() -> None:
    assert _pyproject()["tool"]["hatch"]["version"]["path"] == VERSION_HOME


def test_the_declared_version_home_defines_the_version() -> None:
    """A path hatchling cannot resolve would otherwise only fail at build time."""
    source = (REPO_ROOT / VERSION_HOME).read_text(encoding="utf-8")
    assert "__version__ = " in source
