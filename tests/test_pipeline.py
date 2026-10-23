"""run_pipeline / ``verimend run``: collect -> extract -> reconcile on one crawl_run (design 5.2)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from extractor_fakes import FakeClient, claims, http, port_claim, session, transport

from verimend import __main__ as cli
from verimend import pipeline as pipeline_module
from verimend.db import connection, migrate
from verimend.extractor import ExtractorConfigError
from verimend.pipeline import doc_paths, run_pipeline
from verimend.targets import parse_targets

REPO = {
    "README.md": "# Demo\n\nThe service listens on port 8120.\n",
    "docs/guide/setup.md": "# Setup\n\nRun it.\n",
    "docs/top.md": "# Top\n\nx\n",
    "notes.txt": "not a doc\n",
    "src/demo/config.py": (
        "from pydantic_settings import BaseSettings\n\nclass Settings(BaseSettings):\n    port: int = 8120\n"
    ),
}


def _targets():
    return parse_targets(
        {
            "version": 1,
            "targets": [
                {"repo": "o/demo", "doc_globs": ["README.md", "docs/**/*.md"], "extractors": {"ports": True}}
            ],
        }
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


def _checkout(source):
    root, commit = source

    def checkout(repo: str, dest: Path) -> str:
        shutil.copytree(root, dest)
        return commit

    return checkout


def _port_on_line_3(start: int) -> str:
    return claims(port_claim(3, 8120)) if start == 1 else "[]"


def _run(db, source, make_session):
    with connection(db) as conn:
        result = run_pipeline(conn, _targets(), _checkout(source), make_session)
        row = conn.execute("SELECT status, stats_json FROM crawl_run WHERE id = ?", (result.run_id,)).fetchone()
        claim_count = conn.execute("SELECT count(*) FROM claim WHERE run_id = ?", (result.run_id,)).fetchone()[0]
        verdicts = conn.execute(
            "SELECT v.result FROM verdict v JOIN claim c ON c.id = v.claim_id WHERE c.run_id = ?", (result.run_id,)
        ).fetchall()
        facts = conn.execute("SELECT count(*) FROM fact WHERE run_id = ?", (result.run_id,)).fetchone()[0]
    return result, row["status"], json.loads(row["stats_json"]), claim_count, [v[0] for v in verdicts], facts


def test_documents_come_from_the_checkout_and_claims_are_reconciled(db, source) -> None:
    calls_by_doc = []

    class Recording(FakeClient):
        def complete(self, messages, tier):
            calls_by_doc.append(messages[1]["content"].split(",")[0])
            return super().complete(messages, tier)

    client = Recording(by_chunk=_port_on_line_3)
    result, status, stats, n_claims, verdicts, facts = _run(db, source, lambda: session(client)[0])
    assert status == "succeeded" and result.status == "succeeded"
    assert sorted(set(calls_by_doc)) == [
        "Document o/demo/README.md", "Document o/demo/docs/guide/setup.md", "Document o/demo/docs/top.md"
    ]
    assert stats["extract"]["documents"] == 3
    assert n_claims == 3  # one per document (the fake answers line 3 for every first chunk)
    assert verdicts.count("verified") == 3  # 8120 is a port fact of the same run
    assert stats["reconcile"]["verified"] == 3
    assert facts >= 1


def test_chunk_failure_makes_the_run_partial_and_still_reconciles(db, source) -> None:
    client = FakeClient(by_chunk=lambda start: http(422))
    result, status, stats, *_ = _run(db, source, lambda: session(client)[0])
    assert status == "partial"
    assert {f["error"] for f in stats["extract"]["failed_chunks"]} == {"http_422"}
    assert "reconcile" in stats


def test_abort_makes_the_run_partial_and_records_why(db, source) -> None:
    client = FakeClient(by_chunk=lambda start: transport())
    _, status, stats, *_ = _run(db, source, lambda: session(client)[0])
    assert status == "partial"
    assert stats["extract"]["aborted"]["reason"] == "consecutive_transient_failures"
    assert len(client.calls) == 6  # three chunks, each tried twice; nothing after the abort
    assert "reconcile" in stats


def test_unwaitable_retry_after_makes_the_run_partial(db, source) -> None:
    client = FakeClient(by_chunk=lambda start: http(429, "3600"))
    _, status, stats, *_ = _run(db, source, lambda: session(client, retry_after_max_s=120)[0])
    assert status == "partial"
    assert stats["extract"]["aborted"]["reason"] == "retry_after_exceeds_max"
    assert stats["extract"]["aborted"]["retry_after_s"] == 3600
    assert len(client.calls) == 1


@pytest.mark.parametrize("status_code", [401, 403])
def test_401_403_fail_the_run_skip_reconcile_and_keep_the_facts(db, source, monkeypatch, status_code) -> None:
    reconciled = []
    monkeypatch.setattr(pipeline_module, "reconcile", lambda *a: reconciled.append(a))
    client = FakeClient(by_chunk=lambda start: http(status_code))
    result, status, stats, _, _, facts = _run(db, source, lambda: session(client)[0])
    assert status == "failed" and result.status == "failed"
    assert stats["extract"]["config_error"] == f"http_{status_code}"
    assert reconciled == []
    assert facts >= 1
    assert len(client.calls) == 1


def test_unset_lexora_url_fails_before_any_llm_call(db, source, monkeypatch) -> None:
    reconciled = []
    monkeypatch.setattr(pipeline_module, "reconcile", lambda *a: reconciled.append(a))

    def make_session():
        raise ExtractorConfigError("lexora_url_unset")

    _, status, stats, n_claims, _, facts = _run(db, source, make_session)
    assert status == "failed"
    assert stats["extract"] == {"config_error": "lexora_url_unset"}
    assert reconciled == [] and n_claims == 0 and facts >= 1


def test_a_failed_run_is_not_a_cache_source(db, source) -> None:
    _run(db, source, lambda: session(FakeClient(by_chunk=lambda s: http(401)))[0])
    client = FakeClient(by_chunk=lambda s: "[]")
    _, status, stats, *_ = _run(db, source, lambda: session(client)[0])
    assert status == "succeeded" and stats["extract"]["cached"] == 0 and len(client.calls) == 3


def test_the_second_run_uses_the_cache(db, source) -> None:
    _run(db, source, lambda: session(FakeClient(by_chunk=_port_on_line_3))[0])
    client = FakeClient()
    _, status, stats, n_claims, verdicts, _ = _run(db, source, lambda: session(client)[0])
    assert client.calls == []
    assert stats["extract"]["cached"] == 3 and n_claims == 3 and verdicts == ["verified"] * 3


# ------------------------------------------------------------------ CLI


def test_cli_run_without_lexora_url_exits_non_zero_and_fails_the_run(tmp_path, source, monkeypatch, capsys) -> None:
    targets = tmp_path / "targets.yaml"
    targets.write_text(
        "version: 1\ntargets:\n  - repo: o/demo\n    doc_globs: [README.md]\n    extractors: {ports: true}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VERIMEND_DB_PATH", str(tmp_path / "cli.sqlite3"))
    monkeypatch.setenv("VERIMEND_TARGETS_PATH", str(targets))
    monkeypatch.delenv("VERIMEND_LEXORA_URL", raising=False)
    monkeypatch.setattr(cli, "github_checkout", lambda *a: _checkout(source))
    assert cli.main(["run"]) != 0
    assert "failed" in capsys.readouterr().out
    with connection(tmp_path / "cli.sqlite3") as conn:
        assert conn.execute("SELECT status FROM crawl_run").fetchone()[0] == "failed"


# ---------------------------------------------------------------- globs


def test_doc_globs_match_recursively_and_stay_in_one_component(make_tree) -> None:
    tree = make_tree({k: v for k, v in REPO.items()} | {"docs/a/b/c.md": "x", "READMExmd": "x"})
    assert doc_paths(tree, ["README.md", "docs/**/*.md"]) == [
        "README.md", "docs/a/b/c.md", "docs/guide/setup.md", "docs/top.md"
    ]
    assert doc_paths(tree, ["docs/*.md"]) == ["docs/top.md"]
