"""判定 1 / 判定 2 around the mender, and their ``decision`` rows (T06, thread T-decide-drift-score).

``Decider`` is what ``mend_stale`` holds when ``decide_mode`` is not ``off``
(``mender_from_settings`` attaches it). With ``decide_mode = off`` there is no
``Decider``: no Lexora call, no ``decision`` row, today's behaviour.

- ``judge_drift`` (判定 1, per verdict, before the draft). A deterministic verdict
  is ``exempt`` without a call. Otherwise one ``/v1/decide`` call over the
  passage and its evidence facts. The ``drift`` row is written and committed at
  once. ``skips`` says whether the verdict is kept from the mender: only in
  ``enforce`` and only on ``skip_drift`` / ``skip_mapping``.
- ``judge_patch`` (判定 2, per mender group, after the draft). One call over the
  diff and the group's evidence facts; ``patch_rows`` returns the rows the caller
  inserts **in the transaction that writes the draft**, each carrying the
  draft's ``patch_sha256``, so a draft never exists without its judgement or the
  other way round.

``mode`` is stored on every row. publish applies only ``enforce`` rows whose
``patch_sha256`` is the current draft's; an ``observe`` row is a record only.

Lexora failures never stop the mender: the client turns every one of them into
``no_signal`` (``verimend.decide.lexora``), which never skips a draft (判定 1)
and routes as 分岐 1 / 2 (判定 2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from verimend.decide.lexora import (
    DRIFT_QUESTIONS,
    PATCH_RISK_QUESTIONS,
    POLICY_DRIFT,
    POLICY_PATCH_RISK,
    QUESTIONS_VERSION_DRIFT,
    QUESTIONS_VERSION_PATCH_RISK,
    DecideClient,
    LexoraDecideClient,
    drift_judgement,
    patch_risk_judgement,
)
from verimend.decide.routing import (
    EXEMPT,
    MODE_ENFORCE,
    MODE_OFF,
    STAGE1_SKIPS,
    STAGE_DRIFT,
    STAGE_PATCH_RISK,
    DriftJudgement,
    Thresholds,
    drift_gate,
    group_stage1,
    route_patch,
)

log = logging.getLogger(__name__)

FACT_STATE_MAX = 1500


def patch_sha256(patch: str) -> str:
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class DriftOutcome:
    judgement: DriftJudgement
    outcome: str
    skips: bool


class Decider:
    """The decide stage of one mender pass. ``mode`` is ``observe`` or ``enforce``."""

    def __init__(
        self,
        client: DecideClient,
        mode: str,
        thresholds: Thresholds,
        *,
        now: Callable[[], str] = _utc_now,
    ) -> None:
        if mode == MODE_OFF:
            raise ValueError("a Decider is never built for decide_mode = off")
        self.client = client
        self.mode = mode
        self.thresholds = thresholds
        self.now = now

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    # ------------------------------------------------------------- 判定 1

    def judge_drift(self, conn: sqlite3.Connection, verdict_id: int, method: str, target: Any) -> DriftOutcome:
        if method == "deterministic":
            judgement = DriftJudgement(EXEMPT)
        else:
            decision = self.client.decide(
                policy=POLICY_DRIFT,
                state=drift_state(target),
                questions=DRIFT_QUESTIONS,
                questions_version=QUESTIONS_VERSION_DRIFT,
            )
            if not decision.signal:
                log.warning("verdict %s: drift judgement has no signal (%s)", verdict_id, decision.error)
            judgement = drift_judgement(decision)
        outcome = drift_gate(judgement, self.thresholds)
        self._insert(conn, verdict_id, STAGE_DRIFT, judgement.decision_id, None, outcome)
        conn.commit()
        return DriftOutcome(judgement, outcome, self.mode == MODE_ENFORCE and outcome in STAGE1_SKIPS)

    # ------------------------------------------------------------- 判定 2

    def judge_patch(self, group: Sequence[Any], patch: str, stage1: Sequence[DriftJudgement]) -> "PatchOutcome":
        decision = self.client.decide(
            policy=POLICY_PATCH_RISK,
            state=patch_state(group, patch),
            questions=PATCH_RISK_QUESTIONS,
            questions_version=QUESTIONS_VERSION_PATCH_RISK,
        )
        if not decision.signal:
            log.warning("verdict %s: patch-risk judgement has no signal (%s)", group[0].verdict_id, decision.error)
        judgement = patch_risk_judgement(decision)
        outcome = route_patch(group_stage1(stage1, self.thresholds), judgement, self.thresholds)
        return PatchOutcome(judgement.decision_id, patch_sha256(patch), outcome)

    def insert_patch_rows(self, conn: sqlite3.Connection, verdict_ids: Sequence[int], outcome: "PatchOutcome") -> None:
        """The 判定 2 rows of a group. The caller commits them with the draft."""
        for verdict_id in verdict_ids:
            self._insert(conn, verdict_id, STAGE_PATCH_RISK, outcome.decision_id, outcome.patch_sha256, outcome.outcome)

    def _insert(
        self,
        conn: sqlite3.Connection,
        verdict_id: int,
        stage: str,
        decision_id: str | None,
        sha: str | None,
        outcome: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO decision (verdict_id, stage, mode, decision_id, patch_sha256, outcome, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (verdict_id, stage, self.mode, decision_id, sha, outcome, self.now()),
        )


@dataclass(frozen=True)
class PatchOutcome:
    decision_id: str | None
    patch_sha256: str
    outcome: str


def decider_from_settings(settings) -> Decider | None:
    """None for ``decide_mode = off`` (no call, no row). The URL is the mender's ``lexora_url``."""
    if settings.decide_mode == MODE_OFF or not settings.lexora_url:
        return None
    return Decider(
        LexoraDecideClient(settings.lexora_url, settings.lexora_timeout_s),
        settings.decide_mode,
        Thresholds.from_settings(settings),
    )


# ------------------------------------------------------------------- states


def _facts_text(facts: Sequence[Any]) -> str:
    out = []
    for fact in facts:
        content = json.dumps(fact.content, ensure_ascii=False, sort_keys=True)
        if len(content) > FACT_STATE_MAX:
            content = content[:FACT_STATE_MAX] + "..."
        out.append(f"- [{fact.kind}] {content}")
    return "\n".join(out)


def drift_state(target: Any) -> str:
    """判定 1's state: the passage (msg-023: ドキュメント該当節) and the code facts mapped to it."""
    start, end = target.anchor
    passage = "\n".join(target.anchor_lines)
    return (
        f"Claim: {target.claim_text}\n\n"
        f"Documentation passage ({target.doc_path}:{start}-{end}):\n{passage}\n\n"
        f"Code facts mapped to it:\n{_facts_text(target.facts)}\n"
    )


def patch_state(group: Sequence[Any], patch: str) -> str:
    """判定 2's state: the diff (msg-023: 修正前後の diff) and the code facts it must stay within."""
    facts: dict[int, Any] = {}
    for target in group:
        for fact in target.facts:
            facts.setdefault(fact.id, fact)
    return f"Diff:\n{patch}\n\nCode facts:\n{_facts_text([facts[i] for i in sorted(facts)])}\n"
