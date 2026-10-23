"""One full run: collect -> extract -> reconcile, on one ``crawl_run`` (docs/design.md 2).

The documents are read from the same checkout the facts are collected from
(``collect_targets``' ``doc_sink``), so a claim and the facts it is compared to
always describe the same commit.

How the run ends (design 5.2):

- the collector's own failures are contained as before (``partial``);
- a chunk failure or an abort of the extraction stage makes the run
  ``partial``; the claims that were extracted are reconciled;
- an ``ExtractorConfigError`` (no ``lexora_url``, HTTP 401 / 403) makes it
  ``failed``, with ``stats.extract.config_error``. The reconciler does not run
  and the facts already stored stay. A failed run is never an extraction cache
  source.
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
from verimend.reconciler import reconcile
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
) -> PipelineResult:
    """Run every stage into one new crawl_run and return how it ended.

    ``make_session`` is called once, after collection and before any chunk is
    built; it raises ``ExtractorConfigError`` when Lexora is not configured.
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
        (stats_json,) = conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run_id,)).fetchone()
        stats = json.loads(stats_json)
    except BaseException:
        conn.rollback()
        finish_run(conn, run_id, STATUS_FAILED, stats)
        raise
    finish_run(conn, run_id, status, stats)
    return PipelineResult(run_id, status, stats)


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
