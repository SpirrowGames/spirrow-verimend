"""T06 DoD 8: the join from Verimend's decision rows into Lexora's decisions.db (schema pinned at 3e19e90)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from verimend.db import connection, migrate
from verimend.decide.evaluate import joined_decisions

SCHEMA = Path(__file__).parent / "data" / "lexora_decisions_schema.sql"


def lexora_db(path: Path, rows: list[tuple]) -> Path:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA.read_text(encoding="utf-8"))
    db.executemany(
        "INSERT INTO decisions (decision_id, policy, state_hash, questions_hash, provider, answers_json,"
        " latency_ms, timestamp, provider_error, shadow_of) VALUES (?, 'verimend.drift', ?, ?, ?, ?, 1, 't', ?, ?)",
        [(i, "0" * 16, "1" * 16, p, a, e, s) for i, p, a, e, s in rows],
    )
    db.commit()
    db.close()
    return path


def test_fixture_schema_names_the_pinned_lexora_commit() -> None:
    assert "3e19e9052c0a29ecc94115afdcc3264fa8555586" in SCHEMA.read_text(encoding="utf-8")


def test_shadow_rows_join_on_shadow_of_and_failures_are_apart(tmp_path: Path) -> None:
    db = tmp_path / "v.sqlite3"
    migrate(db)
    lexora = lexora_db(
        tmp_path / "decisions.db",
        [
            ("ans-1", "null", '{"drift": {"score": 0.0}}', None, None),  # the answering row Verimend saw
            ("sh-1", "jev", '{"drift": {"score": 2.0}}', None, "ans-1"),  # the evaluated answer
            ("ans-2", "null", "{}", None, None),
            ("sh-2", "jev", "{}", "jev:timeout", "ans-2"),  # a failed shadow call
            ("ans-3", "jev", '{"x": 1}', None, None),  # no shadow mode: no shadow row
        ],
    )
    with connection(db) as conn:
        run = conn.execute(
            "INSERT INTO crawl_run (started_at, status, stats_json) VALUES ('t', 'running', '{}')"
        ).lastrowid
        claim = conn.execute(
            "INSERT INTO claim (run_id, doc_repo, doc_path, anchor_start, anchor_end, text, claim_type)"
            " VALUES (?, 'o/r', 'README.md', 1, 1, 'c', 'port')",
            (run,),
        ).lastrowid
        verdict = conn.execute(
            "INSERT INTO verdict (claim_id, result, method, evidence_json) VALUES (?, 'stale', 'llm', '{}')", (claim,)
        ).lastrowid
        for decision_id, outcome in (("ans-1", "no_signal"), ("ans-2", "no_signal"), ("ans-3", "generated"), (None, "exempt")):
            conn.execute(
                "INSERT INTO decision (verdict_id, stage, mode, decision_id, outcome, created_at)"
                " VALUES (?, 'drift', 'observe', ?, ?, 't')",
                (verdict, decision_id, outcome),
            )
        conn.commit()
        joined = joined_decisions(conn, lexora)
    by_id = {j.decision_id: j for j in joined}
    assert set(by_id) == {"ans-1", "ans-2", "ans-3"}  # a row that never reached Lexora is not joined
    assert by_id["ans-1"].answered_by == "null" and by_id["ans-1"].answers_json == '{"drift": {"score": 2.0}}'
    assert not by_id["ans-1"].shadow_failed
    assert by_id["ans-2"].shadow_failed and by_id["ans-2"].provider_error == "jev:timeout"
    assert by_id["ans-3"].shadow_id is None
