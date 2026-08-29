"""config/targets.yaml must load, and must reject malformed configuration."""

from pathlib import Path

import pytest
import yaml

from verimend.targets import (
    ExtractorName,
    TargetsConfigError,
    load_targets,
    parse_targets,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID = {
    "version": 1,
    "targets": [
        {
            "repo": "SpirrowGames/spirrow-magickit",
            "doc_globs": ["README.md", "docs/**/*.md"],
            "extractors": {"mcp_schema": True, "ports": False},
        }
    ],
}


def _with_target(**overrides) -> dict:
    target = dict(VALID["targets"][0])
    target.update(overrides)
    return {"version": 1, "targets": [target]}


def test_shipped_targets_file_is_valid() -> None:
    """The config committed to the repository must itself pass validation."""
    config = load_targets(REPO_ROOT / "config" / "targets.yaml")
    repos = [t.repo for t in config.targets]
    assert repos == ["SpirrowGames/spirrow-magickit", "SpirrowGames/spirrow-voxelworld"]

    magickit = config.get("SpirrowGames/spirrow-magickit")
    assert magickit is not None
    assert set(magickit.enabled_extractors) == set(ExtractorName)

    voxelworld = config.get("SpirrowGames/spirrow-voxelworld")
    assert voxelworld is not None
    assert ExtractorName.MCP_SCHEMA not in voxelworld.enabled_extractors
    assert ExtractorName.PORTS in voxelworld.enabled_extractors


def test_valid_document_parses() -> None:
    config = parse_targets(VALID)
    assert config.version == 1
    assert config.targets[0].enabled_extractors == (ExtractorName.MCP_SCHEMA,)


def test_load_from_file(tmp_path: Path) -> None:
    path = tmp_path / "targets.yaml"
    path.write_text(yaml.safe_dump(VALID), encoding="utf-8")
    assert load_targets(path).targets[0].repo == "SpirrowGames/spirrow-magickit"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        pytest.param({"targets": VALID["targets"]}, "version", id="missing-version"),
        pytest.param({"version": 2, "targets": VALID["targets"]}, "version", id="unsupported-version"),
        pytest.param({"version": 1, "targets": []}, "targets", id="no-targets"),
        pytest.param({"version": 1}, "targets", id="missing-targets"),
        pytest.param(
            {"version": 1, "targets": VALID["targets"], "schedule": "nightly"},
            "schedule",
            id="unknown-top-level-key",
        ),
        pytest.param(_with_target(repo="spirrow-magickit"), "repo", id="repo-without-owner"),
        pytest.param(_with_target(repo="Spirrow Games/magickit"), "repo", id="repo-with-space"),
        pytest.param(_with_target(doc_globs=[]), "doc_globs", id="empty-doc-globs"),
        pytest.param(_with_target(doc_globs=["/etc/passwd"]), "doc_globs", id="absolute-doc-glob"),
        pytest.param(_with_target(doc_globs=["../other/README.md"]), "doc_globs", id="escaping-doc-glob"),
        pytest.param(_with_target(extractors={"sql_schema": True}), "sql_schema", id="unknown-extractor"),
        pytest.param(_with_target(extractors={}), "extractor", id="no-extractors"),
        pytest.param(
            _with_target(extractors={"mcp_schema": False, "ports": False}),
            "extractor",
            id="all-extractors-disabled",
        ),
        pytest.param(_with_target(branch="main"), "branch", id="unknown-target-key"),
        pytest.param(
            {"version": 1, "targets": VALID["targets"] * 2},
            "duplicate",
            id="duplicate-repo",
        ),
    ],
)
def test_invalid_documents_are_rejected(raw: dict, reason: str) -> None:
    with pytest.raises(TargetsConfigError) as excinfo:
        parse_targets(raw)
    assert reason in str(excinfo.value)


def test_non_mapping_document_is_rejected() -> None:
    with pytest.raises(TargetsConfigError):
        parse_targets(["SpirrowGames/spirrow-magickit"])


def test_broken_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "targets.yaml"
    path.write_text("version: 1\ntargets: [\n", encoding="utf-8")
    with pytest.raises(TargetsConfigError):
        load_targets(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TargetsConfigError):
        load_targets(tmp_path / "absent.yaml")
