"""extract_documents: claim/document rows and the incremental cache (design 4, 5.2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from extractor_fakes import FakeClient, claims, http, port_claim, session, transport

from verimend.db import connection, migrate
from verimend.extractor import Document, extract_documents
from verimend.extractor import chunking

DOC = Document("o/r", "README.md", "# Ports\nThe API listens on 8113.\n")
OTHER = Document("o/r", "docs/b.md", "# B\nport 9000\n")


@pytest.fixture
def conn(tmp_path: Path):
    migrate(tmp_path / "v.sqlite3")
    with connection(tmp_path / "v.sqlite3") as c:
        yield c


def _run(conn: sqlite3.Connection, status: str = "running") -> int:
    run_id = conn.execute("INSERT INTO crawl_run (started_at, status) VALUES ('t', ?)", (status,)).lastrowid
    conn.commit()
    return run_id


def _finish(conn: sqlite3.Connection, run_id: int, status: str = "succeeded") -> None:
    conn.execute("UPDATE crawl_run SET status = ? WHERE id = ?", (status, run_id))
    conn.commit()


def _ok() -> FakeClient:
    return FakeClient(by_chunk=lambda s: claims(port_claim(s + 1, 8113)))


def _claims(conn: sqlite3.Connection, run_id: int) -> list[tuple]:
    return conn.execute(
        "SELECT doc_path, anchor_start, anchor_end, claim_type, subject_json, extraction_model FROM claim"
        " WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()


def _doc_status(conn: sqlite3.Connection, run_id: int) -> dict[str, str]:
    return dict(conn.execute("SELECT doc_path, status FROM document WHERE run_id = ?", (run_id,)).fetchall())


def test_extracted_claims_and_document_rows_are_stored(conn) -> None:
    run_id = _run(conn)
    s, _ = session(_ok())
    stats = extract_documents(conn, run_id, [DOC], s)
    assert [tuple(r) for r in _claims(conn, run_id)] == [
        ("README.md", 2, 2, "port", '{"polarity":"current","port":8113,"service":null}', "fake-model")
    ]
    assert _doc_status(conn, run_id) == {"README.md": "extracted"}
    assert stats == {"documents": 1, "extracted": 1, "cached": 0, "failed": 0, "claims": 1, "failed_chunks": []}


def test_unchanged_document_is_copied_from_the_previous_run_without_verdicts(conn) -> None:
    first = _run(conn)
    extract_documents(conn, first, [DOC], session(_ok())[0])
    claim_id = conn.execute("SELECT id FROM claim WHERE run_id = ?", (first,)).fetchone()[0]
    conn.execute(
        "INSERT INTO verdict (claim_id, result, method, evidence_json) VALUES (?, 'verified', 'deterministic', '{}')",
        (claim_id,),
    )
    _finish(conn, first)

    second = _run(conn)
    client = FakeClient()
    stats = extract_documents(conn, second, [DOC], session(client)[0])
    assert client.calls == []
    assert [tuple(r) for r in _claims(conn, second)] == [tuple(r) for r in _claims(conn, first)]
    assert conn.execute(
        "SELECT count(*) FROM verdict v JOIN claim c ON c.id = v.claim_id WHERE c.run_id = ?", (second,)
    ).fetchone()[0] == 0
    assert _doc_status(conn, second) == {"README.md": "cached"}
    assert (stats["cached"], stats["claims"]) == (1, 1)


def test_a_cached_copy_is_itself_a_cache_source(conn) -> None:
    for _ in range(2):
        run_id = _run(conn)
        extract_documents(conn, run_id, [DOC], session(_ok())[0])
        _finish(conn, run_id)
    third = _run(conn)
    client = FakeClient()
    extract_documents(conn, third, [DOC], session(client)[0])
    assert client.calls == [] and len(_claims(conn, third)) == 1


def test_changed_content_misses_the_cache(conn) -> None:
    first = _run(conn)
    extract_documents(conn, first, [DOC], session(_ok())[0])
    _finish(conn, first)
    second = _run(conn)
    client = _ok()
    extract_documents(conn, second, [Document(DOC.repo, DOC.path, DOC.text + "More.\n")], session(client)[0])
    assert len(client.calls) == 1
    assert _doc_status(conn, second) == {"README.md": "extracted"}


def test_changed_extractor_key_misses_the_cache(conn) -> None:
    first = _run(conn)
    extract_documents(conn, first, [DOC], session(_ok())[0])
    _finish(conn, first)
    for kwargs in ({"prompt": "another prompt"}, {"tier": "heavy"}):
        run_id = _run(conn)
        client = _ok()
        extract_documents(conn, run_id, [DOC], session(client, **kwargs)[0])
        assert len(client.calls) == 1, kwargs
        _finish(conn, run_id, "failed")  # keep the first run the only source


def test_changed_chunk_boundaries_miss_the_cache_and_unchanged_ones_hit(conn, monkeypatch) -> None:
    first = _run(conn)
    extract_documents(conn, first, [DOC], session(_ok())[0])
    _finish(conn, first)

    # A rule change that does not move any boundary of this document: still a hit.
    monkeypatch.setattr(chunking.chunk_document, "__defaults__", (1500,))
    run_id = _run(conn)
    client = FakeClient()
    extract_documents(conn, run_id, [DOC], session(client)[0])
    assert client.calls == []
    _finish(conn, run_id, "failed")

    # A rule change that moves a boundary: a miss, though text and key are the same.
    monkeypatch.setattr(chunking.chunk_document, "__defaults__", (7,))  # splits the 8-token section in two
    run_id = _run(conn)
    client = FakeClient(by_chunk=lambda s: "[]")
    extract_documents(conn, run_id, [DOC], session(client)[0])
    assert client.chunk_starts() == [1, 2]
    assert _doc_status(conn, run_id) == {"README.md": "extracted"}


def test_a_document_with_a_failed_chunk_is_never_a_cache_source(conn) -> None:
    doc = Document("o/r", "d.md", "# A\nport 1\n# B\nport 2\n")
    first = _run(conn)
    stats = extract_documents(conn, first, [doc], session(FakeClient(claims(port_claim(2, 1)), http(422)))[0])
    assert _doc_status(conn, first) == {"d.md": "failed"}
    assert len(_claims(conn, first)) == 1  # the good chunk's claim is kept
    assert stats["failed_chunks"] == [{"doc_repo": "o/r", "doc_path": "d.md", "lines": [3, 4], "error": "http_422"}]
    _finish(conn, first, "partial")

    second = _run(conn)
    client = FakeClient("[]", "[]")
    extract_documents(conn, second, [doc], session(client)[0])
    assert len(client.calls) == 2


def test_aborted_documents_are_failed_and_recorded(conn) -> None:
    run_id = _run(conn)
    s, _ = session(FakeClient(*[transport()] * 6))
    docs = [Document("o/r", f"{i}.md", "# T\nx\n") for i in range(4)]
    stats = extract_documents(conn, run_id, docs, s)
    assert set(_doc_status(conn, run_id).values()) == {"failed"}
    assert [f["error"] for f in stats["failed_chunks"]] == ["transport"] * 3 + ["extractor_aborted"]
    assert stats["aborted"]["reason"] == "consecutive_transient_failures"
    assert _claims(conn, run_id) == []


@pytest.mark.parametrize("status", ["failed", "running"])
def test_a_failed_or_unfinished_run_is_never_a_cache_source(conn, status: str) -> None:
    first = _run(conn)
    extract_documents(conn, first, [DOC], session(_ok())[0])
    _finish(conn, first, status)
    second = _run(conn)
    client = _ok()
    extract_documents(conn, second, [DOC], session(client)[0])
    assert len(client.calls) == 1


def test_a_partial_run_is_a_source_for_its_complete_documents(conn) -> None:
    first = _run(conn)
    extract_documents(conn, first, [DOC, OTHER], session(FakeClient(claims(port_claim(2, 8113)), http(400)))[0])
    _finish(conn, first, "partial")
    second = _run(conn)
    client = FakeClient("[]")
    extract_documents(conn, second, [DOC, OTHER], session(client)[0])
    assert client.chunk_starts() == [1]
    assert _doc_status(conn, second) == {"README.md": "cached", "docs/b.md": "extracted"}
