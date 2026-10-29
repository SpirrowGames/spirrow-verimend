"""run_pipeline / ``verimend run``: collect -> extract -> reconcile on one crawl_run (design 5.2)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from extractor_fakes import FakeClient, claims, http, port_claim, session, transport

from verimend import __main__ as cli
from verimend import pipeline as pipeline_module
from verimend.db import connection, migrate
from verimend.extractor import Completion, ExtractorConfigError
from verimend.mender import MenderConfigError, MenderSession
from verimend.reconciler import JudgeSession
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


def _run(db, source, make_session, make_judge=None, make_mender=None):
    with connection(db) as conn:
        result = run_pipeline(
            conn, _targets(), _checkout(source), make_session, make_judge=make_judge, make_mender=make_mender
        )
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


class Closable(FakeClient):
    closed = 0

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("reply", ["[]", http(401)], ids=["success", "config-error"])
def test_the_pipeline_closes_the_session_client(db, source, reply) -> None:
    client = Closable(by_chunk=lambda start: reply)
    _run(db, source, lambda: session(client)[0])
    assert client.closed == 1


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


# ------------------------------------------------------- the LLM stage


def _historical_on_line_3(start: int) -> str:
    """A claim the checkers defer (historical), so it reaches the LLM judge."""
    return claims(port_claim(3, 8120, polarity="historical")) if start == 1 else "[]"


def _judge(client) -> JudgeSession:
    return JudgeSession(client, prompt="judge prompt", sleep=lambda s: None)


def _unverifiable() -> str:
    return json.dumps({"result": "unverifiable", "fact_ids": [], "reason": "no evidence either way"})


def test_the_llm_stage_judges_the_claims_the_checkers_deferred(db, source) -> None:
    judge_client = FakeClient(*[_unverifiable()] * 3)
    _, status, stats, n_claims, verdicts, _ = _run(
        db, source, lambda: session(FakeClient(by_chunk=_historical_on_line_3))[0], lambda: _judge(judge_client)
    )
    assert status == "succeeded"
    assert len(judge_client.calls) == 3 and verdicts == ["unverifiable"] * 3
    assert stats["reconcile"]["llm_unverifiable"] == 3 and stats["reconcile"]["deferred"] == 0
    assert stats["reconcile"]["llm_failed"] == []


def test_a_claim_failure_in_the_llm_stage_makes_the_run_partial(db, source) -> None:
    judge_client = FakeClient("x", "y", _unverifiable(), _unverifiable())
    _, status, stats, *_ = _run(
        db, source, lambda: session(FakeClient(by_chunk=_historical_on_line_3))[0], lambda: _judge(judge_client)
    )
    assert status == "partial"
    assert [f["error"] for f in stats["reconcile"]["llm_failed"]] == ["schema"]


@pytest.mark.parametrize("status_code", [401, 404])
def test_a_reconciler_config_error_makes_the_run_partial_and_keeps_the_extraction_cache(
    db, source, status_code
) -> None:
    judge_client = FakeClient(http(status_code))
    result, status, stats, n_claims, verdicts, _ = _run(
        db, source, lambda: session(FakeClient(by_chunk=_historical_on_line_3))[0], lambda: _judge(judge_client)
    )
    assert status == "partial" and result.status == "partial"
    assert stats["reconcile"]["llm_config_error"]["status"] == status_code
    assert len(judge_client.calls) == 1  # the other claims are not sent
    assert n_claims == 3 and verdicts == [] and stats["reconcile"]["deferred"] == 3

    # The run is partial, not failed, so its extraction is a cache source for the next run.
    extract_client = FakeClient()
    _, status2, stats2, *_ = _run(
        db, source, lambda: session(extract_client)[0], lambda: _judge(FakeClient(*[_unverifiable()] * 3))
    )
    assert extract_client.calls == [] and stats2["extract"]["cached"] == 3 and status2 == "succeeded"


def test_an_extraction_404_fails_the_run_and_never_builds_the_judge(db, source) -> None:
    built = []
    _, status, stats, *_ = _run(
        db, source, lambda: session(FakeClient(by_chunk=lambda s: http(404)))[0], lambda: built.append(1)
    )
    assert status == "failed" and stats["extract"]["config_error"] == "http_404" and built == []


def test_three_400s_in_the_llm_stage_abort_it(db, source) -> None:
    judge_client = FakeClient(http(400), http(400), http(400))
    _, status, stats, *_ = _run(
        db, source, lambda: session(FakeClient(by_chunk=_historical_on_line_3))[0], lambda: _judge(judge_client)
    )
    assert status == "partial"
    assert stats["reconcile"]["llm_aborted"]["reason"] == "permanent_http_streak"
    assert stats["reconcile"]["llm_aborted"]["status"] == 400


def test_three_400s_in_the_extraction_stage_abort_it(db, source) -> None:
    _, status, stats, *_ = _run(db, source, lambda: session(FakeClient(by_chunk=lambda s: http(400)))[0])
    assert status == "partial"
    assert stats["extract"]["aborted"]["reason"] == "permanent_http_streak"
    assert stats["extract"]["aborted"]["status"] == 400


# ------------------------------------------------------------------ CLI


class StaleJudge(FakeClient):
    """Judges every claim stale on the first candidate fact it is shown."""

    def complete(self, messages, tier):
        self.calls.append(messages)
        fact_id = int(re.search(r'"fact_id": (\d+)', messages[1]["content"]).group(1))
        return Completion(text=json.dumps({"result": "stale", "fact_ids": [fact_id], "reason": "r"}), model="m")


def _mender(client) -> MenderSession:
    return MenderSession(client, prompt="mender prompt", sleep=lambda s: None)


def _drafts(db, run_id):
    with connection(db) as conn:
        return conn.execute(
            "SELECT v.patch_draft FROM verdict v JOIN claim c ON c.id = v.claim_id WHERE c.run_id = ? ORDER BY v.id",
            (run_id,),
        ).fetchall()


def test_the_mender_drafts_the_stale_verdicts_from_the_checkout_text(db, source) -> None:
    mender_client = FakeClient(*[json.dumps({"lines": ["fixed"]})] * 3)
    result, status, stats, _, verdicts, _ = _run(
        db, source, lambda: session(FakeClient(by_chunk=_historical_on_line_3))[0],
        lambda: _judge(StaleJudge()), lambda: _mender(mender_client),
    )
    assert status == "succeeded" and verdicts == ["stale"] * 3
    assert len(mender_client.calls) == 3
    assert "The service listens on port 8120." in mender_client.calls[0][1]["content"]
    assert stats["mend"]["drafted"] == 3 and stats["mend"]["skipped"]["doc_changed"] == 0
    drafts = [row[0] for row in _drafts(db, result.run_id)]
    assert all(d and "\n+fixed" in d for d in drafts)  # the checkout's line endings are kept (CRLF on Windows)
    assert "\n-The service listens on port 8120." in drafts[0]


def test_a_mender_failure_makes_the_run_partial_and_keeps_the_verdicts(db, source) -> None:
    mender_client = FakeClient(http(400), http(400), http(400))
    _, status, stats, _, verdicts, _ = _run(
        db, source, lambda: session(FakeClient(by_chunk=_historical_on_line_3))[0],
        lambda: _judge(StaleJudge()), lambda: _mender(mender_client),
    )
    assert status == "partial" and verdicts == ["stale"] * 3
    assert stats["mend"]["aborted"]["reason"] == "permanent_http_streak"


def test_a_mender_schema_streak_makes_the_run_partial_with_its_own_reason(db, source) -> None:
    mender_client = FakeClient(*["not json", "still not json"] * 3)
    _, status, stats, _, verdicts, _ = _run(
        db, source, lambda: session(FakeClient(by_chunk=_historical_on_line_3))[0],
        lambda: _judge(StaleJudge()), lambda: _mender(mender_client),
    )
    assert status == "partial" and verdicts == ["stale"] * 3
    assert stats["mend"]["aborted"]["reason"] == "permanent_schema_streak"
    assert "status" not in stats["mend"]["aborted"]


def test_an_unbuildable_mender_is_a_config_error_and_the_run_partial(db, source) -> None:
    def no_url():
        raise MenderConfigError("lexora_url_unset")

    _, status, stats, _, verdicts, _ = _run(
        db, source, lambda: session(FakeClient(by_chunk=_historical_on_line_3))[0],
        lambda: _judge(StaleJudge()), no_url,
    )
    assert status == "partial" and verdicts == ["stale"] * 3
    assert stats["mend"] == {"config_error": {"reason": "lexora_url_unset"}}


def test_no_mender_means_no_mend_stats(db, source) -> None:
    _, status, stats, *_ = _run(
        db, source, lambda: session(FakeClient(by_chunk=_historical_on_line_3))[0], lambda: _judge(StaleJudge())
    )
    assert status == "succeeded" and "mend" not in stats


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


def test_cli_run_exits_1_on_a_reconciler_config_error(tmp_path, source, monkeypatch, capsys) -> None:
    targets = tmp_path / "targets.yaml"
    targets.write_text(
        "version: 1\ntargets:\n  - repo: o/demo\n    doc_globs: [README.md]\n    extractors: {ports: true}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VERIMEND_DB_PATH", str(tmp_path / "cli.sqlite3"))
    monkeypatch.setenv("VERIMEND_TARGETS_PATH", str(targets))
    monkeypatch.setattr(cli, "github_checkout", lambda *a: _checkout(source))
    extract_client = FakeClient(by_chunk=_historical_on_line_3)
    monkeypatch.setattr(cli, "session_from_settings", lambda s: session(extract_client)[0])
    monkeypatch.setattr(cli, "judge_from_settings", lambda s: _judge(FakeClient(http(401))))
    assert cli.main(["run"]) == 1
    assert "partial" in capsys.readouterr().out


# ---------------------------------------------------------------- globs


def test_doc_globs_match_recursively_and_stay_in_one_component(make_tree) -> None:
    tree = make_tree({k: v for k, v in REPO.items()} | {"docs/a/b/c.md": "x", "READMExmd": "x"})
    assert doc_paths(tree, ["README.md", "docs/**/*.md"]) == [
        "README.md", "docs/a/b/c.md", "docs/guide/setup.md", "docs/top.md"
    ]
    assert doc_paths(tree, ["docs/*.md"]) == ["docs/top.md"]
