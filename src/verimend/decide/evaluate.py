"""Join Verimend's ``decision`` rows to Lexora's ``decisions.db`` for offline evaluation (T06 DoD 8).

Lexora's schema is pinned at ``3e19e9052c0a29ecc94115afdcc3264fa8555586``
(``src/lexora/decide/log.py``; the test fixture is a verbatim copy). Under
Lexora's shadow mode the row that answered Verimend is ``provider = 'null'``
and the provider being evaluated wrote a **shadow row** whose ``shadow_of`` is
that answering row's ``decision_id``. So the evaluation answers come from
``decisions.shadow_of = decision.decision_id``; a shadow row with
``provider_error`` set is a failed call (``answers_json = '{}'``) and is counted
apart, never read as an answer.

Read-only on both databases: Lexora's file is opened ``mode=ro`` on its own connection.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

CHUNK = 500  # well under SQLite's bound-parameter limit


@dataclass(frozen=True)
class JoinedDecision:
    verdict_id: int
    stage: str
    mode: str
    outcome: str
    decision_id: str
    answered_by: str
    shadow_id: str | None
    shadow_provider: str | None
    answers_json: str | None
    provider_error: str | None

    @property
    def shadow_failed(self) -> bool:
        return self.shadow_id is not None and self.provider_error is not None


def joined_decisions(conn: sqlite3.Connection, lexora_db: Path | str) -> list[JoinedDecision]:
    """Every Verimend decision that reached Lexora, with its shadow rows (one row per shadow, or none).

    The join is ``answering.decision_id = decision.decision_id AND answering.shadow_of IS NULL``
    and ``shadow.shadow_of = decision.decision_id``.
    """
    ours = conn.execute(
        "SELECT verdict_id, stage, mode, outcome, decision_id FROM decision WHERE decision_id IS NOT NULL ORDER BY id"
    ).fetchall()
    ids = sorted({row[4] for row in ours})
    answered: dict[str, str] = {}
    shadows: dict[str, list[tuple]] = {}
    lexora = sqlite3.connect(Path(lexora_db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        for i in range(0, len(ids), CHUNK):
            part = ids[i : i + CHUNK]
            marks = ",".join("?" * len(part))
            for decision_id, provider in lexora.execute(
                f"SELECT decision_id, provider FROM decisions WHERE shadow_of IS NULL AND decision_id IN ({marks})",
                part,
            ):
                answered[decision_id] = provider
            for row in lexora.execute(
                f"SELECT shadow_of, decision_id, provider, answers_json, provider_error FROM decisions"
                f" WHERE shadow_of IN ({marks}) ORDER BY decision_id",
                part,
            ):
                shadows.setdefault(row[0], []).append(row[1:])
    finally:
        lexora.close()
    out: list[JoinedDecision] = []
    for verdict_id, stage, mode, outcome, decision_id in ours:
        if decision_id not in answered:
            continue
        head = (verdict_id, stage, mode, outcome, decision_id, answered[decision_id])
        for shadow in shadows.get(decision_id) or [(None, None, None, None)]:
            out.append(JoinedDecision(*head, *shadow))
    return out
