"""The extraction stage of a run: documents in, ``claim`` and ``document`` rows out.

Incremental extraction (docs/design.md 4): a document whose
``(doc_repo, doc_path, content_hash, extractor_key, chunk_hash)`` matches a
complete extraction of an earlier run gets that run's claims copied -- without
verdicts -- instead of a call to the LLM.

- ``content_hash`` -- the document text changed.
- ``extractor_key`` -- the prompt, the tier or the output schema changed.
- ``chunk_hash`` -- the chunk boundaries changed (the chunking rules, say),
  even when the text did not.

A cache source must be complete: a ``document`` row of status ``extracted`` or
``cached`` (a document with any failed or aborted chunk is ``failed`` and never
a source, so it is extracted again next run), in a run that did not fail (a
``failed`` run is never a source; ``running`` is the current one).

Each document's claims and its ``document`` row are committed together.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from typing import Any

from verimend.extractor.chunking import chunk_hash
from verimend.extractor.extract import Document, ExtractionSession

log = logging.getLogger(__name__)

DOC_EXTRACTED = "extracted"
DOC_CACHED = "cached"
DOC_FAILED = "failed"


def extract_documents(
    conn: sqlite3.Connection,
    run_id: int,
    documents: Iterable[Document],
    session: ExtractionSession,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract (or copy from the cache) every document into run ``run_id``. Returns ``stats.extract``.

    The counters go into ``stats`` when given, so a caller catching an error
    keeps what was counted before it. ``ExtractorConfigError`` (and
    ``ExtractorAborted`` in strict mode) propagate; documents committed before
    stay.
    """
    stats = {} if stats is None else stats
    stats.update({"documents": 0, "extracted": 0, "cached": 0, "failed": 0, "claims": 0, "failed_chunks": []})
    for document in documents:
        stats["documents"] += 1
        chunks = session.chunks(document)
        key = (document.repo, document.path, document.content_hash, session.extractor_key)
        layout = chunk_hash(chunks)
        source = _cache_source(conn, run_id, *key, layout)
        if source is not None:
            copied = _copy_claims(conn, source, run_id, document)
            status = DOC_CACHED
            stats["claims"] += copied
        else:
            extraction = session.extract(document, chunks)
            for c in extraction.claims:
                conn.execute(
                    "INSERT INTO claim (run_id, doc_repo, doc_path, anchor_start, anchor_end, text, claim_type,"
                    " extraction_model, subject_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, document.repo, document.path, c.anchor_start, c.anchor_end, c.text, c.claim_type,
                     c.model, c.subject_json),
                )
            stats["claims"] += len(extraction.claims)
            stats["failed_chunks"].extend(f.as_stats() for f in extraction.failures)
            status = DOC_FAILED if extraction.failures else DOC_EXTRACTED
        conn.execute(
            "INSERT INTO document (run_id, doc_repo, doc_path, content_hash, extractor_key, chunk_hash, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, *key, layout, status),
        )
        conn.commit()
        stats[status] += 1
    if session.aborted is not None:
        stats["aborted"] = session.aborted
    return stats


def _cache_source(
    conn: sqlite3.Connection, run_id: int, repo: str, path: str, content_hash: str, key: str, layout: str
) -> int | None:
    row = conn.execute(
        """
        SELECT d.run_id FROM document AS d JOIN crawl_run AS r ON r.id = d.run_id
        WHERE d.doc_repo = ? AND d.doc_path = ? AND d.content_hash = ? AND d.extractor_key = ?
          AND d.chunk_hash = ? AND d.status IN (?, ?) AND r.status IN ('succeeded', 'partial') AND d.run_id != ?
        ORDER BY d.run_id DESC LIMIT 1
        """,
        (repo, path, content_hash, key, layout, DOC_EXTRACTED, DOC_CACHED, run_id),
    ).fetchone()
    return None if row is None else row[0]


def _copy_claims(conn: sqlite3.Connection, source_run: int, run_id: int, document: Document) -> int:
    cursor = conn.execute(
        """
        INSERT INTO claim (run_id, doc_repo, doc_path, anchor_start, anchor_end, text, claim_type,
                           extraction_model, subject_json)
        SELECT ?, doc_repo, doc_path, anchor_start, anchor_end, text, claim_type, extraction_model, subject_json
        FROM claim WHERE run_id = ? AND doc_repo = ? AND doc_path = ? ORDER BY id
        """,
        (run_id, source_run, document.repo, document.path),
    )
    return cursor.rowcount
