"""collect(): one crawl_run, facts stored per target, failures contained."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from verimend.collector import collect
from verimend.collector import extractors as extractors_module
from verimend.collector import run as run_module
from verimend.db import connection, migrate
from verimend.targets import ExtractorName, parse_targets

REPO = {
    "pyproject.toml": '[project]\nname = "demo"\n\n[project.scripts]\ndemo = "demo.main:main"\n',
    "src/demo/__init__.py": "",
    "src/demo/config.py": (
        "from pydantic_settings import BaseSettings, SettingsConfigDict\n\n"
        "class Settings(BaseSettings):\n"
        '    model_config = SettingsConfigDict(env_prefix="DEMO_")\n'
        "    port: int = 8120\n"
    ),
    "src/demo/tools.py": "@mcp.tool()\ndef ping(x: int) -> str:\n    '''Ping.'''\n",
}


def _targets(*repos: str, extractors=None):
    extractors = extractors or {"mcp_schema": True, "config_keys": True, "entrypoints": True, "ports": True}
    return parse_targets(
        {"version": 1, "targets": [{"repo": r, "doc_globs": ["README.md"], "extractors": extractors} for r in repos]}
    ).targets


@pytest.fixture
def source(tmp_path: Path, write_repo) -> tuple[Path, str]:
    root = tmp_path / "source"
    return root, write_repo(root, REPO)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "v.sqlite3"
    migrate(path)
    return path


def _copying_checkout(source_root: Path, commit: str):
    def checkout(repo: str, dest: Path) -> str:
        shutil.copytree(source_root, dest)
        return commit

    return checkout


def test_stores_facts_from_every_enabled_extractor(source, db) -> None:
    root, commit = source
    with connection(db) as conn:
        result = collect(conn, _targets("o/demo"), _copying_checkout(root, commit))

        assert result.status == "succeeded"
        counts = result.stats["targets"]["o/demo"]["extractors"]
        assert counts == {
            "mcp_schema": {"facts": 1},
            "config_keys": {"facts": 1},
            "entrypoints": {"facts": 1},
            "ports": {"facts": 1},
        }
        rows = conn.execute("SELECT * FROM fact WHERE run_id = ?", (result.run_id,)).fetchall()
        run = conn.execute("SELECT * FROM crawl_run WHERE id = ?", (result.run_id,)).fetchone()

    assert {r["source_kind"] for r in rows} == {"tool_schema", "config", "file"}
    tool = next(r for r in rows if r["source_kind"] == "tool_schema")
    assert tool["source_ref"] == f"o/demo@{commit}:src/demo/tools.py:2"  # the def line, below the decorator
    assert json.loads(tool["content"])["tool"] == "ping"
    assert run["status"] == "succeeded"
    assert run["finished_at"] is not None
    assert json.loads(run["stats_json"]) == result.stats


def test_service_health_without_a_source_is_an_error_not_a_skip(source, db) -> None:
    """There is no "not implemented yet" path left: a missing source fails loudly."""
    root, commit = source
    targets = _targets("o/demo", extractors={"ports": True, "service_health": True})
    with connection(db) as conn:
        result = collect(conn, targets, _copying_checkout(root, commit))

    assert result.status == "partial"
    assert "error" in result.stats["targets"]["o/demo"]["extractors"]["service_health"]


def test_failing_extractor_loses_only_its_own_facts(source, db, monkeypatch) -> None:
    root, commit = source

    def half_then_boom(tree):
        yield from extractors_module.REGISTRY[ExtractorName.PORTS](tree)
        raise RuntimeError("boom")

    registry = dict(extractors_module.REGISTRY)
    registry[ExtractorName.MCP_SCHEMA] = half_then_boom
    monkeypatch.setattr(extractors_module, "REGISTRY", registry)

    with connection(db) as conn:
        result = collect(conn, _targets("o/demo"), _copying_checkout(root, commit))
        kinds = {r["source_kind"] for r in conn.execute("SELECT source_kind FROM fact")}

    extractors = result.stats["targets"]["o/demo"]["extractors"]
    assert result.status == "partial"
    assert extractors["mcp_schema"] == {"error": "RuntimeError: boom"}
    assert extractors["ports"] == {"facts": 1}
    assert "tool_schema" not in kinds  # nothing of the failed extractor was stored
    # ports stored exactly once: the half-run of the failing extractor left nothing behind
    with connection(db) as conn:
        assert conn.execute("SELECT count(*) FROM fact WHERE source_kind = 'file'").fetchone()[0] == 1


def test_failed_checkout_loses_only_that_target(source, db) -> None:
    root, commit = source
    good = _copying_checkout(root, commit)

    def checkout(repo: str, dest: Path) -> str:
        if repo == "o/missing":
            raise subprocess.CalledProcessError(128, ["git", "clone"], stderr="fatal: repository not found\n")
        return good(repo, dest)

    with connection(db) as conn:
        result = collect(conn, _targets("o/missing", "o/demo"), checkout)

    assert result.status == "partial"
    assert result.stats["targets"]["o/missing"] == {"error": "checkout failed: exit 128: fatal: repository not found"}
    assert result.stats["targets"]["o/demo"]["extractors"]["mcp_schema"] == {"facts": 1}


def test_unexpected_error_marks_the_run_failed(source, db) -> None:
    root, commit = source

    def checkout(repo: str, dest: Path) -> str:
        raise KeyboardInterrupt

    with connection(db) as conn:
        with pytest.raises(KeyboardInterrupt):
            collect(conn, _targets("o/demo"), checkout)
        run = conn.execute("SELECT status, finished_at FROM crawl_run").fetchone()

    assert run["status"] == "failed"
    assert run["finished_at"] is not None


def test_github_checkout_clones_a_local_repository(source, tmp_path) -> None:
    root, commit = source
    # A file:// base URL exercises the real clone path without the network.
    bare = tmp_path / "remote" / "o" / "demo.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(root), str(bare)], check=True)
    checkout = run_module.github_checkout(base_url=(tmp_path / "remote").as_uri(), timeout=60)

    dest = tmp_path / "clone"
    assert checkout("o/demo", dest) == commit
    assert (dest / "src" / "demo" / "tools.py").exists()


def test_cli_rejects_a_repo_that_is_not_a_target(monkeypatch, tmp_path, capsys) -> None:
    from verimend.__main__ import main

    monkeypatch.setenv("VERIMEND_DB_PATH", str(tmp_path / "v.sqlite3"))
    assert main(["collect", "--repo", "o/nope"]) == 2
    assert "not a target" in capsys.readouterr().out
