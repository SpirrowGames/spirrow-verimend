"""The reconciler's LLM stage (verimend/reconciler/llm.py, design 5.3, M2c). The LLM is a fake."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from extractor_fakes import FakeClient, http, transport

from verimend.collector.facts import canonical_json
from verimend.db import connection, migrate
from verimend.reconciler import JudgeAborted, JudgeConfigError, JudgeSession, judge_deferred, reconcile
from verimend.reconciler.llm import validate_judgment
from verimend.settings import Settings

REPO = "SpirrowGames/spirrow-magickit"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "verimend.sqlite3"
    migrate(db)
    with connection(db) as c:
        yield c


def _run(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "INSERT INTO crawl_run (started_at, status, stats_json) VALUES ('2026-10-23T00:00:00Z', 'running', '{}')"
    ).lastrowid


def _fact(conn: sqlite3.Connection, run_id: int, content: dict[str, Any], repo: str = REPO) -> int:
    return conn.execute(
        "INSERT INTO fact (run_id, source_kind, source_ref, content, content_hash) VALUES (?, 'file', ?, ?, 'h')",
        (run_id, f"{repo}@abc:src/x.py:1", canonical_json(content)),
    ).lastrowid


def _claim(
    conn: sqlite3.Connection, run_id: int, text: str, claim_type: str = "behavior", subject: Any = None
) -> int:
    subject_json = subject if subject is None or isinstance(subject, str) else json.dumps(subject)
    return conn.execute(
        "INSERT INTO claim (run_id, doc_repo, doc_path, anchor_start, anchor_end, text, claim_type, subject_json)"
        " VALUES (?, ?, 'README.md', 3, 3, ?, ?, ?)",
        (run_id, REPO, text, claim_type, subject_json),
    ).lastrowid


def _verdict(conn: sqlite3.Connection, claim_id: int) -> tuple[str, str, dict] | None:
    row = conn.execute(
        "SELECT result, method, evidence_json FROM verdict WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    return None if row is None else (row[0], row[1], json.loads(row[2]))


def _stats(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    (stats_json,) = conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run_id,)).fetchone()
    return json.loads(stats_json)["reconcile"]


def _judge(client: Any, **kwargs: Any) -> JudgeSession:
    kwargs.setdefault("prompt", "judge prompt")
    return JudgeSession(client, sleep=lambda s: None, **kwargs)


def answer(result: str, fact_ids: list[int], reason: str = "because") -> str:
    return json.dumps({"result": result, "fact_ids": fact_ids, "reason": reason})


def _setup(conn, n_claims: int = 1) -> tuple[int, int, list[int]]:
    """A run with one fact about the health check and ``n_claims`` claims that match it."""
    run = _run(conn)
    fact = _fact(conn, run, {"extractor": "entrypoints", "kind": "console_script", "name": "magickit-health"})
    claims = [_claim(conn, run, f"magickit-health reports status {i}") for i in range(n_claims)]
    conn.commit()
    return run, fact, claims


def _both_stages(conn, run, judge) -> dict:
    reconcile(conn, run)
    return judge_deferred(conn, run, judge)


# ------------------------------------------------------------- retrieval


def test_japanese_claim_reaches_the_llm_instead_of_no_candidates(conn) -> None:
    run = _run(conn)
    fact = _fact(conn, run, {"extractor": "config_keys", "description": "設定ファイルの場所"})
    claim = _claim(conn, run, "設定ファイルは var に置く")
    conn.commit()
    client = FakeClient(answer("verified", [fact]))
    _both_stages(conn, run, _judge(client))
    assert len(client.calls) == 1
    assert _verdict(conn, claim)[0] == "verified"


def test_no_candidate_facts_settles_unverifiable_without_calling_the_llm(conn) -> None:
    run = _run(conn)
    _fact(conn, run, {"extractor": "ports", "port": 8113})
    claim = _claim(conn, run, "deployments are approved by a human")
    conn.commit()
    client = FakeClient()
    passed = _both_stages(conn, run, _judge(client))
    assert client.calls == []
    assert _verdict(conn, claim) == ("unverifiable", "llm", {"judge": "llm", "reason": "no_candidate_facts"})
    assert passed["no_candidates"] == 1


def test_facts_of_another_repository_are_not_candidates(conn) -> None:
    run = _run(conn)
    _fact(conn, run, {"name": "magickit-health"}, repo="SpirrowGames/spirrow-voxelworld")
    claim = _claim(conn, run, "magickit-health exists")
    conn.commit()
    client = FakeClient()
    _both_stages(conn, run, _judge(client))
    assert client.calls == [] and _verdict(conn, claim)[2]["reason"] == "no_candidate_facts"


# ------------------------------------------------------------- judgement


def test_a_verdict_is_written_with_method_llm_and_its_evidence(conn) -> None:
    run, fact, (claim,) = _setup(conn)
    client = FakeClient(answer("stale", [fact], "the command is named differently"))
    _both_stages(conn, run, _judge(client, tier="light"))
    assert _verdict(conn, claim) == (
        "stale",
        "llm",
        {"judge": "llm", "tier": "light", "fact_ids": [fact], "reason": "the command is named differently"},
    )
    prompt = client.calls[0][1]["content"]
    assert "magickit-health reports status 0" in prompt and f'"fact_id": {fact}' in prompt
    assert "README.md, lines 3-3" in prompt
    stats = _stats(conn, run)
    assert stats["llm_stale"] == 1 and stats["stale"] == 1 and stats["deferred"] == 0
    assert stats["llm_failed"] == [] and "llm_aborted" not in stats


def test_a_long_fact_is_cut_to_1500_characters_in_the_prompt(conn) -> None:
    run = _run(conn)
    _fact(conn, run, {"name": "magickit-health", "blob": "x" * 5000})
    _claim(conn, run, "magickit-health exists")
    conn.commit()
    client = FakeClient(answer("unverifiable", []))
    _both_stages(conn, run, _judge(client))
    assert "...(truncated)" in client.calls[0][1]["content"]
    assert "x" * 1600 not in client.calls[0][1]["content"]


def test_reason_is_cut_to_300_characters(conn) -> None:
    run, fact, (claim,) = _setup(conn)
    _both_stages(conn, run, _judge(FakeClient(answer("verified", [fact], "r" * 1000))))
    assert len(_verdict(conn, claim)[2]["reason"]) == 300


@pytest.mark.parametrize(
    "bad",
    ["not json", "[]", '{"result": "verified", "fact_ids": []}', '{"result": "maybe", "fact_ids": [], "reason": ""}',
     '{"result": "stale", "fact_ids": ["1"], "reason": ""}', '{"result": "stale", "fact_ids": [1], "reason": 3}'],
    ids=["not-json", "array", "missing-key", "unknown-result", "string-id", "reason-type"],
)
def test_a_frame_violation_is_retried_once_with_the_violations(conn, bad: str) -> None:
    run, fact, (claim,) = _setup(conn)
    client = FakeClient(bad, answer("verified", [fact]))
    _both_stages(conn, run, _judge(client))
    assert len(client.calls) == 2
    assert "rejected" in client.calls[1][-1]["content"]
    assert _verdict(conn, claim)[0] == "verified"


def test_a_frame_violation_twice_leaves_the_claim_deferred_and_records_it(conn) -> None:
    run, fact, (claim,) = _setup(conn)
    _both_stages(conn, run, _judge(FakeClient("x", "y")))
    assert _verdict(conn, claim) is None
    stats = _stats(conn, run)
    assert stats["llm_failed"] == [{"claim_id": claim, "error": "schema"}]
    assert stats["deferred"] == 1


def test_a_code_fence_around_the_object_is_tolerated() -> None:
    parsed, errors = validate_judgment('```json\n{"result": "verified", "fact_ids": [1], "reason": "x"}\n```')
    assert errors == [] and parsed["result"] == "verified"


# ------------------------------------------------------ hallucination guard


def test_fact_ids_that_were_not_offered_are_dropped(conn) -> None:
    run, fact, (claim,) = _setup(conn)
    _both_stages(conn, run, _judge(FakeClient(answer("stale", [fact, 9999]))))
    assert _verdict(conn, claim)[2]["fact_ids"] == [fact]


@pytest.mark.parametrize("result", ["stale", "verified"])
def test_a_verdict_without_evidence_is_downgraded_to_unverifiable(conn, result: str) -> None:
    run, fact, (claim,) = _setup(conn)
    _both_stages(conn, run, _judge(FakeClient(answer(result, [9999]))))
    got, method, evidence = _verdict(conn, claim)
    assert (got, method) == ("unverifiable", "llm")
    assert evidence["downgraded"] is True and evidence["fact_ids"] == []
    stats = _stats(conn, run)
    assert stats["llm_downgraded"] == 1 and stats["llm_unverifiable"] == 1


def test_unverifiable_without_evidence_is_not_a_downgrade(conn) -> None:
    run, fact, (claim,) = _setup(conn)
    _both_stages(conn, run, _judge(FakeClient(answer("unverifiable", []))))
    assert "downgraded" not in _verdict(conn, claim)[2]


# ------------------------------------------------ what reaches the judge


def test_an_invalid_subject_never_reaches_the_llm(conn) -> None:
    run = _run(conn)
    _fact(conn, run, {"extractor": "ports", "port": 8113})
    claim = _claim(conn, run, "listens on 8113", "port", '{"polarity": "current", "port": "8113"}')
    conn.commit()
    client = FakeClient()
    _both_stages(conn, run, _judge(client))
    assert client.calls == []
    assert _verdict(conn, claim)[:2] == ("unverifiable", "deterministic")


def test_a_deterministically_decided_claim_is_not_judged_again(conn) -> None:
    run = _run(conn)
    _fact(conn, run, {"extractor": "ports", "port": 8113})
    decided = _claim(conn, run, "listens on 8113", "port", {"polarity": "current", "port": 8113, "service": None})
    deferred = _claim(conn, run, "port 8113 used to be 8000", "port",
                      {"polarity": "historical", "port": 8000, "service": None})
    conn.commit()
    client = FakeClient(answer("unverifiable", []))
    _both_stages(conn, run, _judge(client))
    assert len(client.calls) == 1 and "used to be" in client.calls[0][1]["content"]
    assert _verdict(conn, decided)[:2] == ("verified", "deterministic")
    assert _verdict(conn, deferred)[:2] == ("unverifiable", "llm")


# --------------------------------------------------------- failure classes


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_configuration_error_stops_the_stage_and_keeps_earlier_verdicts(conn, status: int) -> None:
    run, fact, claims = _setup(conn, 3)
    client = FakeClient(answer("verified", [fact]), http(status))
    passed = _both_stages(conn, run, _judge(client))
    assert len(client.calls) == 2  # the third claim is never sent
    assert _verdict(conn, claims[0])[0] == "verified"
    assert _verdict(conn, claims[1]) is None and _verdict(conn, claims[2]) is None
    stats = _stats(conn, run)
    assert stats["llm_config_error"] == {"status": status, "at_claim": claims[1]}
    assert stats["deferred"] == 2 and passed["skipped"] == 2


def test_a_transient_failure_is_retried_once_then_fails_the_claim(conn) -> None:
    run, fact, claims = _setup(conn, 2)
    client = FakeClient(http(503), transport(), answer("verified", [fact]))
    _both_stages(conn, run, _judge(client))
    assert _verdict(conn, claims[0]) is None and _verdict(conn, claims[1])[0] == "verified"
    assert _stats(conn, run)["llm_failed"] == [{"claim_id": claims[0], "error": "transport"}]


def test_three_transient_claim_failures_in_a_row_abort(conn) -> None:
    run, fact, claims = _setup(conn, 4)
    client = FakeClient(*[http(503)] * 6)
    _both_stages(conn, run, _judge(client))
    assert len(client.calls) == 6  # claim 4 is never sent
    stats = _stats(conn, run)
    assert stats["llm_aborted"] == {"reason": "consecutive_transient_failures", "at_claim": claims[2]}
    assert stats["deferred"] == 4 and len(stats["llm_failed"]) == 3


def test_an_unwaitable_retry_after_aborts_at_once(conn) -> None:
    run, fact, claims = _setup(conn, 2)
    client = FakeClient(http(429, "3600"))
    _both_stages(conn, run, _judge(client, retry_after_max_s=120))
    assert len(client.calls) == 1
    assert _stats(conn, run)["llm_aborted"] == {
        "reason": "retry_after_exceeds_max", "at_claim": claims[0], "retry_after_s": 3600,
    }


def test_three_permanent_4xx_in_a_row_abort_and_stop_calling(conn) -> None:
    run, fact, claims = _setup(conn, 5)
    client = FakeClient(http(400), http(400), http(400))
    _both_stages(conn, run, _judge(client))
    assert len(client.calls) == 3  # claims 4 and 5 are never sent
    stats = _stats(conn, run)
    assert stats["llm_aborted"] == {"reason": "permanent_http_streak", "at_claim": claims[2], "status": 400}
    assert [f["error"] for f in stats["llm_failed"]] == ["http_400"] * 3


def test_a_success_resets_the_permanent_streak(conn) -> None:
    run, fact, claims = _setup(conn, 4)
    client = FakeClient(http(400), answer("verified", [fact]), http(400), http(400))
    _both_stages(conn, run, _judge(client))
    assert "llm_aborted" not in _stats(conn, run) and len(client.calls) == 4


def test_schema_failures_are_not_in_the_permanent_streak(conn) -> None:
    run, fact, claims = _setup(conn, 4)
    client = FakeClient(http(400), "x", "y", "x", "y", http(400))
    _both_stages(conn, run, _judge(client))
    stats = _stats(conn, run)
    assert "llm_aborted" not in stats
    assert [f["error"] for f in stats["llm_failed"]] == ["http_400", "schema", "schema", "http_400"]


# ------------------------------------------------------------- strict (eval)


def test_strict_raises_a_config_error(conn) -> None:
    run, fact, _ = _setup(conn)
    with pytest.raises(JudgeConfigError) as info:
        _both_stages(conn, run, _judge(FakeClient(http(404)), strict=True))
    assert info.value.reason == "http_404"


def test_strict_raises_on_a_single_transient_claim_failure(conn) -> None:
    run, fact, _ = _setup(conn, 2)
    with pytest.raises(JudgeAborted):
        _both_stages(conn, run, _judge(FakeClient(http(503), http(503)), strict=True))


def test_strict_raises_on_a_permanent_streak(conn) -> None:
    run, fact, _ = _setup(conn, 3)
    with pytest.raises(JudgeAborted) as info:
        _both_stages(conn, run, _judge(FakeClient(http(400), http(400), http(400)), strict=True))
    assert info.value.reason == "permanent_http_streak"


def test_strict_keeps_a_single_permanent_failure_as_a_deferred_claim(conn) -> None:
    run, fact, claims = _setup(conn, 2)
    _both_stages(conn, run, _judge(FakeClient(http(422), answer("verified", [fact])), strict=True))
    assert _verdict(conn, claims[0]) is None and _verdict(conn, claims[1])[0] == "verified"


# ---------------------------------------------------------------- settings


def test_reconciler_tier_defaults_to_light_and_rejects_empty(monkeypatch) -> None:
    monkeypatch.delenv("VERIMEND_RECONCILER_TIER", raising=False)
    assert Settings().reconciler_tier == "light"
    monkeypatch.setenv("VERIMEND_RECONCILER_TIER", "")
    with pytest.raises(ValueError):
        Settings()


def test_the_judge_prompt_ships_with_the_package() -> None:
    from verimend.reconciler.llm import load_prompt

    prompt = load_prompt()
    assert "fact_ids" in prompt and "unverifiable" in prompt
