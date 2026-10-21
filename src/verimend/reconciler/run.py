"""One reconciliation pass over a run's claims (docs/design.md 5.3, M2a: deterministic only).

``reconcile`` takes every claim of the run that has no verdict yet and decides
each one from its structured subject, against the facts of the same run and
the same repository. A claim ends the pass in exactly one of these states:

=========================================  =====================================  =================
subject                                    outcome                                counted as
=========================================  =====================================  =================
NULL, or a type with no checker            no verdict (left for the LLM stage)    ``deferred``
not JSON, or violates its schema           verdict ``unverifiable`` /             ``invalid_subject``
                                           ``deterministic``, ``log.error``
valid, ``polarity`` not ``current``        no verdict                             ``deferred``
valid, checker undecided (None)            no verdict                             ``deferred``
valid, checker decided                     verdict ``verified`` / ``stale``       ``verified`` /
                                                                                  ``stale``
=========================================  =====================================  =================

An invalid subject is an extractor fault. It is settled on the spot rather than
deferred, so it can never be handed to the LLM stage, and it is settled per
claim rather than raised, so one broken claim cannot fail the run (the
containment rule of ``collector/run.py``; design 5.2 "schema violations are
recorded as unverifiable, the pipeline does not stop").

Re-running is safe: a claim with a verdict is not considered again, and
``verdict.claim_id`` is UNIQUE besides.

After the pass the run's ``stats_json`` gets a ``reconcile`` entry with the
totals *for the run* (recomputed from the tables, so a re-run does not erase
an earlier pass's ``invalid_subject``). ``ReconcileResult.stats`` counts this
pass only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from verimend.claims import SubjectError, parse_subject
from verimend.collector.facts import canonical_json
from verimend.reconciler.checkers import CHECKERS, FactRow, check, load_fact

log = logging.getLogger(__name__)

SUBJECT_SCHEMA_VIOLATION = "subject_schema_violation"
STAT_KEYS = ("considered", "verified", "stale", "deferred", "invalid_subject")


@dataclass(frozen=True)
class ReconcileResult:
    run_id: int
    stats: dict[str, int]


def reconcile(conn: sqlite3.Connection, run_id: int) -> ReconcileResult:
    """Write deterministic verdicts for the run's undecided claims. Commits on success."""
    if conn.execute("SELECT 1 FROM crawl_run WHERE id = ?", (run_id,)).fetchone() is None:
        raise ValueError(f"no crawl_run with id {run_id}")

    claims = conn.execute(
        """
        SELECT c.id, c.doc_repo, c.claim_type, c.subject_json
        FROM claim AS c LEFT JOIN verdict AS v ON v.claim_id = c.id
        WHERE c.run_id = ? AND v.id IS NULL
        ORDER BY c.id
        """,
        (run_id,),
    ).fetchall()

    stats = dict.fromkeys(STAT_KEYS, 0)
    facts_by_repo: dict[str, list[FactRow]] = {}
    for claim_id, repo, claim_type, subject_json in claims:
        stats["considered"] += 1
        try:
            subject = parse_subject(claim_type, subject_json)
        except SubjectError as exc:
            log.error("claim %s (%s, run %s): invalid subject: %s", claim_id, claim_type, run_id, exc)
            _write(
                conn,
                claim_id,
                "unverifiable",
                {"checker": claim_type, "error": SUBJECT_SCHEMA_VIOLATION, "detail": str(exc)},
            )
            stats["invalid_subject"] += 1
            continue

        if subject is None or subject.polarity != "current" or claim_type not in CHECKERS:
            stats["deferred"] += 1
            continue

        if repo not in facts_by_repo:
            facts_by_repo[repo] = _repo_facts(conn, run_id, repo)
        decision = check(claim_type, subject, facts_by_repo[repo])
        if decision is None:
            stats["deferred"] += 1
            continue
        _write(conn, claim_id, decision.result, decision.evidence(claim_type))
        stats[decision.result] += 1

    _record_totals(conn, run_id)
    conn.commit()
    return ReconcileResult(run_id=run_id, stats=stats)


def _repo_facts(conn: sqlite3.Connection, run_id: int, repo: str) -> Sequence[FactRow]:
    """Facts of ``repo`` in this run: those whose ``source_ref`` is ``<repo>@...``.

    A prefix comparison, not LIKE: ``_`` in a repository name is a LIKE
    wildcard. Facts not tied to a repository (``magickit:service_health/...``)
    never match.
    """
    prefix = f"{repo}@"
    rows = conn.execute(
        "SELECT id, content FROM fact WHERE run_id = ? AND substr(source_ref, 1, ?) = ? ORDER BY id",
        (run_id, len(prefix), prefix),
    )
    return [load_fact(row_id, content) for row_id, content in rows]


def _write(conn: sqlite3.Connection, claim_id: int, result: str, evidence: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO verdict (claim_id, result, method, evidence_json) VALUES (?, ?, 'deterministic', ?)",
        (claim_id, result, canonical_json(evidence)),
    )


def _record_totals(conn: sqlite3.Connection, run_id: int) -> None:
    totals = dict.fromkeys(("verified", "stale", "unverifiable", "deferred", "invalid_subject"), 0)
    for result, evidence_json in conn.execute(
        "SELECT v.result, v.evidence_json FROM verdict AS v JOIN claim AS c ON c.id = v.claim_id WHERE c.run_id = ?",
        (run_id,),
    ):
        totals[result] += 1
        if json.loads(evidence_json).get("error") == SUBJECT_SCHEMA_VIOLATION:
            totals["invalid_subject"] += 1
    totals["deferred"] = conn.execute(
        "SELECT count(*) FROM claim AS c LEFT JOIN verdict AS v ON v.claim_id = c.id"
        " WHERE c.run_id = ? AND v.id IS NULL",
        (run_id,),
    ).fetchone()[0]

    (stats_json,) = conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run_id,)).fetchone()
    stats = json.loads(stats_json) if stats_json else {}
    stats["reconcile"] = totals
    conn.execute("UPDATE crawl_run SET stats_json = ? WHERE id = ?", (canonical_json(stats), run_id))
