"""One full run: collect -> extract -> reconcile (deterministic, then LLM), on one ``crawl_run`` (design 2).

The documents are read from the same checkout the facts are collected from
(``collect_targets``' ``doc_sink``), so a claim and the facts it is compared to
always describe the same commit.

How the run ends (design 5.2):

- the collector's own failures are contained as before (``partial``);
- a chunk failure or an abort of the extraction stage makes the run
  ``partial``; the claims that were extracted are reconciled;
- an ``ExtractorConfigError`` (no ``lexora_url``, HTTP 401 / 403 / 404) makes
  it ``failed``, with ``stats.extract.config_error``. The reconciler does not
  run and the facts already stored stay. A failed run is never an extraction
  cache source.
- every failure of the reconciler's LLM stage -- a claim failure, an abort,
  even a configuration error (401 / 403 / 404) -- makes the run ``partial``,
  never ``failed``: extraction succeeded before it, and failing the run would
  stop it being a cache source (``verimend.reconciler.llm``). The claims it
  could not judge stay ``deferred``; the verdicts written stay.
- the mender (M3a, ``verimend.mender``) runs last, on the same documents; any
  Lexora failure of it -- a verdict failure, an abort, a configuration error
  -- makes the run ``partial`` the same way. Drafts already written stay.
- any other error marks the run ``failed`` and propagates.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from verimend.collector.facts import canonical_json
from verimend.collector.health import HealthSource
from verimend.collector.run import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SUCCEEDED,
    Checkout,
    collect_targets,
    finish_run,
    has_errors,
    start_run,
)
from verimend.collector.tree import RepoTree
from verimend.extractor import Document, ExtractionSession, ExtractorConfigError, extract_documents
from verimend.mender import MenderConfigError, MenderSession, mend_stage_failed, mend_stale
from verimend.reconciler import JudgeConfigError, JudgeSession, judge_deferred, llm_stage_failed, reconcile
from verimend.targets import Target

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineResult:
    run_id: int
    status: str
    stats: dict[str, Any]


def run_pipeline(
    conn: sqlite3.Connection,
    targets: Sequence[Target],
    checkout: Checkout,
    make_session: Callable[[], ExtractionSession],
    health_source: HealthSource | None = None,
    make_judge: Callable[[], JudgeSession] | None = None,
    make_mender: Callable[[], MenderSession] | None = None,
) -> PipelineResult:
    """Run every stage into one new crawl_run and return how it ended.

    ``make_session`` is called once, after collection and before any chunk is
    built; it raises ``ExtractorConfigError`` when Lexora is not configured.
    ``make_judge`` is called once, after the deterministic stage; without it
    the LLM stage does not run and undecided claims stay ``deferred``.
    ``make_mender`` is called once, after the reconciler; without it no patch
    draft is written.
    """
    run_id = start_run(conn)
    stats: dict[str, Any] = {"targets": {}}
    documents: list[Document] = []

    def read_documents(target: Target, tree: RepoTree, commit: str) -> None:
        for path in doc_paths(tree, target.doc_globs):
            text = tree.text(path)
            if text is not None:  # an unreadable file is in the tree's skipped_files
                documents.append(Document(target.repo, path, text))

    try:
        collect_targets(conn, run_id, targets, checkout, health_source, read_documents, stats=stats)
        extract: dict[str, Any] = {}
        stats["extract"] = extract
        try:
            with make_session() as session:
                extract_documents(conn, run_id, documents, session, stats=extract)
        except ExtractorConfigError as exc:
            conn.rollback()
            log.error("extraction stage: configuration error %s; the run fails and is not reconciled", exc.reason)
            extract["config_error"] = exc.reason
            finish_run(conn, run_id, STATUS_FAILED, stats)
            return PipelineResult(run_id, STATUS_FAILED, stats)

        failed = bool(extract.get("failed_chunks")) or "aborted" in extract
        status = STATUS_PARTIAL if failed or has_errors(stats) else STATUS_SUCCEEDED
        conn.execute("UPDATE crawl_run SET stats_json = ? WHERE id = ?", (canonical_json(stats), run_id))
        reconcile(conn, run_id)
        if make_judge is not None:
            _judge(conn, run_id, make_judge)
        if make_mender is not None:
            _mend(conn, run_id, documents, make_mender)
        (stats_json,) = conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run_id,)).fetchone()
        stats = json.loads(stats_json)
        if llm_stage_failed(stats.get("reconcile", {})) or mend_stage_failed(stats.get("mend", {})):
            status = STATUS_PARTIAL
    except BaseException:
        conn.rollback()
        finish_run(conn, run_id, STATUS_FAILED, stats)
        raise
    finish_run(conn, run_id, status, stats)
    return PipelineResult(run_id, status, stats)


def _judge(conn: sqlite3.Connection, run_id: int, make_judge: Callable[[], JudgeSession]) -> None:
    """The LLM stage. A judge that cannot even be built is recorded like a configuration error."""
    try:
        judge = make_judge()
    except JudgeConfigError as exc:
        log.error("LLM judge: configuration error %s; deferred claims stay deferred", exc.reason)
        (stats_json,) = conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run_id,)).fetchone()
        stats = json.loads(stats_json)  # start_run wrote '{}': never NULL here
        stats.setdefault("reconcile", {})["llm_config_error"] = {"reason": exc.reason}
        conn.execute("UPDATE crawl_run SET stats_json = ? WHERE id = ?", (canonical_json(stats), run_id))
        conn.commit()
        return
    with judge:
        judge_deferred(conn, run_id, judge)


def _mend(
    conn: sqlite3.Connection, run_id: int, documents: Sequence[Document], make_mender: Callable[[], MenderSession]
) -> None:
    """The mender stage. A mender that cannot even be built is recorded like a configuration error."""
    try:
        mender = make_mender()
    except MenderConfigError as exc:
        log.error("mender: configuration error %s; no patch drafts", exc.reason)
        (stats_json,) = conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run_id,)).fetchone()
        stats = json.loads(stats_json)
        stats["mend"] = {"config_error": {"reason": exc.reason}}
        conn.execute("UPDATE crawl_run SET stats_json = ? WHERE id = ?", (canonical_json(stats), run_id))
        conn.commit()
        return
    with mender:
        mend_stale(conn, run_id, documents, mender)


def doc_paths(tree: RepoTree, globs: Sequence[str]) -> list[str]:
    """Files of ``tree`` matching any of the repository-relative ``globs``.

    ``*`` and ``?`` stay within one path component; ``**/`` matches zero or
    more whole directories. (``PurePath.match`` cannot do the latter before
    Python 3.13, and CI runs 3.11.)
    """
    patterns = [_glob_regex(g) for g in globs]
    return [f for f in tree.files if any(p.fullmatch(f) for p in patterns)]


@lru_cache(maxsize=None)
def _glob_regex(glob: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out))
