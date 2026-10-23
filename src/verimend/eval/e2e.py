"""End-to-end evaluation: extractor + deterministic reconciler on a golden case (docs/design.md 8).

``run_e2e`` puts a case's frozen facts into an in-memory database, extracts
the case's ``doc.md`` with a *strict* ``ExtractionSession``, reconciles, and
scores the resulting claims on the ``deterministic`` layer (the LLM judge does
not exist yet, so the final layer would count every claim it should decide as
wrong).

The session must be strict. A transient failure (Lexora briefly down, rate
limited) or a configuration error says nothing about the extractor, and a
score computed over it would read as lost recall; strict mode raises
``ExtractorAborted`` / ``ExtractorConfigError`` instead, before ``score`` is
called. Permanent chunk failures (``schema``, a 4xx, ``oversized_line``) are
the extractor's own and are scored.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from verimend.db.migrate import apply_migrations
from verimend.eval import golden
from verimend.eval.golden import GoldenCase, Prediction, Score, insert_facts
from verimend.extractor import Document, ExtractionSession, extract_documents
from verimend.reconciler import reconcile

DOC_PATH = "doc.md"


@dataclass(frozen=True)
class E2EResult:
    case_id: str
    score: Score
    predictions: tuple[Prediction, ...]
    extract: dict[str, Any]


def run_e2e(case: GoldenCase, session: ExtractionSession) -> E2EResult:
    """Extract, reconcile and score one case. Raises ``ExtractorAborted`` / ``ExtractorConfigError``."""
    if not session.strict:
        raise ValueError("run_e2e needs a strict ExtractionSession (transient failures must not be scored)")
    text = (case.path / DOC_PATH).read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        apply_migrations(conn)
        run_id = conn.execute(
            "INSERT INTO crawl_run (started_at, status) VALUES ('1970-01-01T00:00:00+00:00', 'running')"
        ).lastrowid
        key_of = insert_facts(conn, run_id, case)
        conn.commit()

        extract = extract_documents(conn, run_id, [Document(case.repo, DOC_PATH, text)], session)
        reconcile(conn, run_id)

        predictions = []
        for row in conn.execute(
            "SELECT c.anchor_start, c.anchor_end, c.claim_type, v.result, v.evidence_json"
            " FROM claim AS c LEFT JOIN verdict AS v ON v.claim_id = c.id WHERE c.run_id = ? ORDER BY c.id",
            (run_id,),
        ):
            evidence = json.loads(row["evidence_json"]) if row["evidence_json"] else {}
            predictions.append(
                Prediction(
                    anchor=(row["anchor_start"], row["anchor_end"]),
                    claim_type=row["claim_type"],
                    result=row["result"],
                    evidence_fact_keys=[key_of[i] for i in evidence.get("fact_ids", []) if i in key_of],
                )
            )
    finally:
        conn.close()

    return E2EResult(
        case_id=case.id,
        score=golden.score(case, predictions, "deterministic"),
        predictions=tuple(predictions),
        extract=extract,
    )
