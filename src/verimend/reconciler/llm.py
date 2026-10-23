"""The reconciler's LLM stage (docs/design.md 5.3, M2c): a verdict for each claim the checkers left.

``judge_deferred(conn, run_id, judge)`` runs after the deterministic stage has
committed. It takes every claim of the run that still has no verdict -- the
``deferred`` ones: a NULL subject, ``polarity != current``, a type with no
checker, a checker that could not decide. A claim with an invalid subject
already has its ``unverifiable`` / ``deterministic`` verdict and is never
seen here; neither is a claim a checker decided.

Per claim:

1. **Retrieval** (``verimend.reconciler.retrieval``). BM25 over the facts of the claim's run and
   repository -- the deterministic stage's own scope. The query is the claim
   text plus the subject's string values; both sides go through
   ``verimend.reconciler.tokenize``. The top five facts with a score above 0
   are the candidates. None at all: the LLM is not called and the verdict is
   ``unverifiable`` / ``llm`` with evidence
   ``{"judge": "llm", "reason": "no_candidate_facts"}``.
2. **Judgement.** Lexora, tier ``reconciler_tier``, answers
   ``{result, fact_ids, reason}``. A frame violation (not JSON, keys missing
   or extra, an unknown ``result``, wrong types) is retried once with the
   violations; if it survives, no verdict is written -- the claim stays
   ``deferred`` and is listed in ``stats.reconcile.llm_failed`` as
   ``{claim_id, error: "schema"}``. ``reason`` is cut to 300 characters.
3. **Hallucination guard.** ``fact_ids`` not among the candidates are
   dropped. A ``stale`` *or* ``verified`` left with no ``fact_ids`` is
   downgraded to ``unverifiable`` (``evidence.downgraded = true``): a verdict
   with no evidence is not kept in either direction.
4. The verdict is written with ``method = llm`` and evidence
   ``{"judge": "llm", "tier", "fact_ids", "reason"[, "downgraded"]}``, and
   committed per claim, so whatever stops the stage later keeps it.

Lexora failures follow ``verimend.llm_policy`` with the claim as the unit.
**Every failure of this stage makes the run ``partial``, never ``failed``**:
the extraction before it succeeded, and a ``failed`` run is never an
extraction cache source, so failing it here would throw away correct
extraction work. ``failed`` is for the extraction stage's configuration
errors only.

==================  =====================================================================
failure             in a run (``strict=False``)
==================  =====================================================================
configuration       401 / 403 / 404: the stage stops, every remaining claim stays
                    ``deferred`` without a call; ``stats.reconcile.llm_config_error =
                    {status, at_claim}``, ``log.error``
transient           retried once; then a claim failure (``llm_failed``,
                    ``http_<status>`` / ``transport``); three claims in a row abort
transient, too      abort at once (``retry_after_exceeds_max``, with ``retry_after_s``)
long to wait
permanent HTTP 4xx  a claim failure; three in a row with no successful Lexora call
                    between abort (``permanent_http_streak``, with ``status``)
==================  =====================================================================

After an abort the remaining claims stay ``deferred`` without a call, and
``stats.reconcile.llm_aborted = {reason, at_claim[, retry_after_s][, status]}``.
``at_claim`` is the claim id.

``strict`` is the evaluation mode: a configuration error raises
``JudgeConfigError``, the first transient claim failure (after its retry) and
any abort raise ``JudgeAborted``, so nothing is scored over them. A permanent
claim failure (a single 4xx, ``schema``) is the judge's own and is scored --
the claim simply has no verdict.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any

from verimend.collector.facts import canonical_json
from verimend.extractor.client import LexoraClient, LLMClient
from verimend.llm_policy import (
    ABORT_CONSECUTIVE,
    ABORT_PERMANENT_HTTP,
    ABORT_RETRY_AFTER,
    CONSECUTIVE_TRANSIENT_LIMIT,
    ConfigStatus,
    Permanent,
    Streaks,
    Transient,
    Unwaitable,
    count_permanent,
    request,
)
from verimend.reconciler.retrieval import FactDoc, query_tokens, search

log = logging.getLogger(__name__)

FACT_CONTENT_MAX = 1500
REASON_MAX = 300
RESULTS = frozenset({"verified", "stale", "unverifiable"})
OUTPUT_KEYS = frozenset({"result", "fact_ids", "reason"})

ERROR_SCHEMA = "schema"
NO_CANDIDATES = "no_candidate_facts"


def load_prompt() -> str:
    return resources.files("verimend.reconciler").joinpath("prompt.md").read_text(encoding="utf-8")


# ------------------------------------------------------------- exceptions


class JudgeConfigError(Exception):
    """Evaluation mode, or no ``lexora_url``: the setup is broken, so no score may be computed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # lexora_url_unset | http_401 | http_403 | http_404


class JudgeAborted(Exception):
    """Evaluation mode only: a transient failure or an abort happened, so no score may be computed."""

    def __init__(self, reason: str, detail: dict[str, Any]) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# ----------------------------------------------------------------- session


@dataclass(frozen=True)
class ClaimToJudge:
    id: int
    doc_repo: str
    doc_path: str
    anchor: tuple[int | None, int | None]
    text: str
    claim_type: str
    subject_json: str | None


@dataclass(frozen=True)
class Judgment:
    result: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class ClaimFailure:
    claim_id: int
    error: str

    def as_stats(self) -> dict[str, Any]:
        return {"claim_id": self.claim_id, "error": self.error}


class JudgeSession:
    """Judge the deferred claims of one run. Holds the cross-claim state (streaks, abort, config error).

    Owns its client like ``ExtractionSession``: close it, or use it as a context manager.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        tier: str = "light",
        retry_after_max_s: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
        strict: bool = False,
        prompt: str | None = None,
    ) -> None:
        self.client = client
        self.tier = tier
        self.retry_after_max_s = retry_after_max_s
        self.sleep = sleep
        self.strict = strict
        self.prompt = prompt if prompt is not None else load_prompt()
        self.streaks = Streaks()
        self.aborted: dict[str, Any] | None = None
        self.config_error: dict[str, Any] | None = None

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "JudgeSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def stopped(self) -> bool:
        return self.aborted is not None or self.config_error is not None

    def judge(self, claim: ClaimToJudge, candidates: Sequence[FactDoc]) -> Judgment | ClaimFailure | None:
        """A verdict, a claim failure, or None when the stage has stopped (the claim stays deferred).

        Raises ``JudgeConfigError`` / ``JudgeAborted`` in strict mode.
        """
        if self.stopped:
            return None
        if not candidates:
            return Judgment("unverifiable", {"judge": "llm", "reason": NO_CANDIDATES})
        try:
            parsed = self._ask(claim, candidates)
        except ConfigStatus as exc:
            log.error("claim %s: LLM judge configuration error %s; the stage stops", claim.id, exc.error)
            if self.strict:
                raise JudgeConfigError(exc.error) from exc
            self.config_error = {"status": exc.status, "at_claim": claim.id}
            return None
        except Unwaitable as exc:
            self._abort(ABORT_RETRY_AFTER, claim.id, retry_after_s=exc.retry_after_s)
            return None
        except Transient as exc:
            self.streaks.transient += 1
            log.warning("claim %s: transient failure %s (%d in a row)", claim.id, exc.error, self.streaks.transient)
            if self.strict:
                raise JudgeAborted(exc.error, {"at_claim": claim.id}) from exc
            if self.streaks.transient >= CONSECUTIVE_TRANSIENT_LIMIT:
                self._abort(ABORT_CONSECUTIVE, claim.id)
            return ClaimFailure(claim.id, exc.error)
        except Permanent as exc:
            log.error("claim %s: %s", claim.id, exc.error)
            if count_permanent(self.streaks, exc):
                self._abort(ABORT_PERMANENT_HTTP, claim.id, status=exc.status)
            return ClaimFailure(claim.id, exc.error)
        self.streaks.transient = 0
        return _guard(parsed, candidates, self.tier, claim.id)

    def _abort(self, reason: str, at_claim: int, **extra: Any) -> None:
        detail = {"reason": reason, "at_claim": at_claim, **{k: v for k, v in extra.items() if v is not None}}
        log.error("LLM judge aborted: %s", detail)
        if self.strict:
            raise JudgeAborted(reason, detail)
        self.aborted = detail

    def _ask(self, claim: ClaimToJudge, candidates: Sequence[FactDoc]) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": render_claim(claim, candidates)},
        ]
        completion = self._request(messages)
        parsed, errors = validate_judgment(completion.text)
        if errors:
            messages += [
                {"role": "assistant", "content": completion.text or ""},
                {"role": "user", "content": _correction(errors)},
            ]
            completion = self._request(messages)
            parsed, errors = validate_judgment(completion.text)
            if errors:
                log.error("claim %s: judgement still malformed after a retry: %s", claim.id, "; ".join(errors[:5]))
                raise Permanent(ERROR_SCHEMA)
        assert parsed is not None
        return parsed

    def _request(self, messages: list[dict[str, str]]):
        return request(
            self.client, messages, self.tier,
            retry_after_max_s=self.retry_after_max_s, sleep=self.sleep, streaks=self.streaks,
        )


def judge_from_settings(settings, **kwargs) -> JudgeSession:
    """A ``JudgeSession`` on Lexora as configured. Raises ``JudgeConfigError`` without a URL."""
    if not settings.lexora_url:
        raise JudgeConfigError("lexora_url_unset")
    client = LexoraClient(settings.lexora_url, settings.lexora_timeout_s)
    return JudgeSession(
        client, tier=settings.reconciler_tier, retry_after_max_s=settings.lexora_retry_after_max_s, **kwargs
    )


# ------------------------------------------------------- output and guard


def validate_judgment(text: str | None) -> tuple[dict[str, Any] | None, list[str]]:
    """``({result, fact_ids, reason}, [])`` or ``(None, violations)``. ``reason`` is cut to 300 characters."""
    if text is None:
        return None, ["the response carried no message content"]
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1])
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, [f"not JSON: {exc}"]
    if not isinstance(data, dict):
        return None, [f"expected a JSON object, got {type(data).__name__}"]
    missing, extra = OUTPUT_KEYS - data.keys(), data.keys() - OUTPUT_KEYS
    if missing or extra:
        return None, [f"missing keys {sorted(missing)}, unexpected keys {sorted(extra)}"]
    errors = []
    if data["result"] not in RESULTS:
        errors.append(f"unknown result {data['result']!r}; use one of {sorted(RESULTS)}")
    ids = data["fact_ids"]
    if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        errors.append("fact_ids must be an array of integers")
    if not isinstance(data["reason"], str):
        errors.append("reason must be a string")
    if errors:
        return None, errors
    return {"result": data["result"], "fact_ids": ids, "reason": data["reason"][:REASON_MAX]}, []


def _guard(parsed: dict[str, Any], candidates: Sequence[FactDoc], tier: str, claim_id: int) -> Judgment:
    """Drop fact ids that were not offered; downgrade an evidence-less verified / stale."""
    offered = {f.id for f in candidates}
    kept = sorted({i for i in parsed["fact_ids"] if i in offered})
    dropped = sorted({i for i in parsed["fact_ids"] if i not in offered})
    if dropped:
        log.warning("claim %s: the judge named fact ids it was not shown, dropped: %s", claim_id, dropped)
    result = parsed["result"]
    evidence: dict[str, Any] = {"judge": "llm", "tier": tier, "fact_ids": kept, "reason": parsed["reason"]}
    if result in ("verified", "stale") and not kept:
        log.warning("claim %s: %s without evidence, downgraded to unverifiable", claim_id, result)
        result = "unverifiable"
        evidence["downgraded"] = True
    return Judgment(result, evidence)


def render_claim(claim: ClaimToJudge, candidates: Sequence[FactDoc]) -> str:
    start, end = claim.anchor
    where = f"{claim.doc_path}, lines {start}-{end}" if start is not None else claim.doc_path
    lines = [f"Claim ({claim.claim_type}) from {where}:", claim.text, "", "Candidate facts:"]
    for f in candidates:
        content = canonical_json(f.content)
        if len(content) > FACT_CONTENT_MAX:
            content = content[:FACT_CONTENT_MAX] + "...(truncated)"
        lines.append(json.dumps({"fact_id": f.id, "kind": f.kind}) + " " + content)
    return "\n".join(lines)


def _correction(errors: Sequence[str]) -> str:
    return (
        "Your answer was rejected for these reasons:\n- "
        + "\n- ".join(errors[:10])
        + "\nAnswer again with only the corrected JSON object."
    )


# -------------------------------------------------------------- the stage


def judge_deferred(conn: sqlite3.Connection, run_id: int, judge: JudgeSession) -> dict[str, Any]:
    """Judge every verdict-less claim of the run. Returns this pass's counters; updates ``stats.reconcile``."""
    claims = [
        ClaimToJudge(row[0], row[1], row[2], (row[3], row[4]), row[5], row[6], row[7])
        for row in conn.execute(
            """
            SELECT c.id, c.doc_repo, c.doc_path, c.anchor_start, c.anchor_end, c.text, c.claim_type, c.subject_json
            FROM claim AS c LEFT JOIN verdict AS v ON v.claim_id = c.id
            WHERE c.run_id = ? AND v.id IS NULL
            ORDER BY c.id
            """,
            (run_id,),
        )
    ]
    passed = {"considered": 0, "judged": 0, "no_candidates": 0, "failed": 0, "skipped": 0}
    failures: list[ClaimFailure] = []
    facts_by_repo: dict[str, list[FactDoc]] = {}
    try:
        for claim in claims:
            passed["considered"] += 1
            if judge.stopped:
                passed["skipped"] += 1
                continue
            if claim.doc_repo not in facts_by_repo:
                facts_by_repo[claim.doc_repo] = _repo_facts(conn, run_id, claim.doc_repo)
            candidates = search(query_tokens(claim.text, claim.subject_json), facts_by_repo[claim.doc_repo])
            outcome = judge.judge(claim, candidates)
            if outcome is None:
                passed["skipped"] += 1
            elif isinstance(outcome, ClaimFailure):
                failures.append(outcome)
                passed["failed"] += 1
            else:
                conn.execute(
                    "INSERT INTO verdict (claim_id, result, method, evidence_json) VALUES (?, ?, 'llm', ?)",
                    (claim.id, outcome.result, canonical_json(outcome.evidence)),
                )
                conn.commit()
                passed["no_candidates" if outcome.evidence.get("reason") == NO_CANDIDATES else "judged"] += 1
    finally:
        _record_llm_totals(conn, run_id, failures, judge)
        conn.commit()
    return passed


def _repo_facts(conn: sqlite3.Connection, run_id: int, repo: str) -> list[FactDoc]:
    """The deterministic stage's scope: facts of this run whose ``source_ref`` starts with ``<repo>@``."""
    prefix = f"{repo}@"
    rows = conn.execute(
        "SELECT id, source_kind, content FROM fact WHERE run_id = ? AND substr(source_ref, 1, ?) = ? ORDER BY id",
        (run_id, len(prefix), prefix),
    )
    return [FactDoc(row_id, kind, json.loads(content)) for row_id, kind, content in rows]


def _record_llm_totals(
    conn: sqlite3.Connection, run_id: int, failures: Sequence[ClaimFailure], judge: JudgeSession
) -> None:
    """``stats.reconcile``: the run's totals recomputed from the tables, plus this pass's failures and stop."""
    from verimend.reconciler.run import record_totals  # run.py imports nothing from here

    record_totals(conn, run_id)
    (stats_json,) = conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run_id,)).fetchone()
    stats = json.loads(stats_json) if stats_json else {}
    reconcile = stats.setdefault("reconcile", {})
    for key in ("llm_verified", "llm_stale", "llm_unverifiable", "llm_downgraded"):
        reconcile.setdefault(key, 0)
    reconcile["llm_failed"] = [f.as_stats() for f in failures]
    for key, value in (("llm_aborted", judge.aborted), ("llm_config_error", judge.config_error)):
        if value is not None:
            reconcile[key] = value
        else:
            reconcile.pop(key, None)
    conn.execute("UPDATE crawl_run SET stats_json = ? WHERE id = ?", (canonical_json(stats), run_id))


def llm_stage_failed(reconcile_stats: dict[str, Any]) -> bool:
    """True when the LLM stage left work undone: a claim failure, an abort or a configuration error."""
    return bool(reconcile_stats.get("llm_failed")) or "llm_aborted" in reconcile_stats or (
        "llm_config_error" in reconcile_stats
    )
