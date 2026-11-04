"""The mender's patch drafts (verimend/mender/draft.py, design 5.4, M3a). The LLM is a fake."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from extractor_fakes import FakeClient, http, transport

from verimend.collector.facts import canonical_json
from verimend.db import connection, migrate
from verimend.extractor import Document
from verimend.mender import MenderAborted, MenderConfigError, MenderSession, build_patch, mend_stale
from verimend.mender.draft import _evidence, check_replacement, load_prompt, validate_draft
from verimend.settings import Settings

REPO = "SpirrowGames/spirrow-magickit"
PATH = "README.md"
TEXT = (
    "# Magickit\n"  # 1
    "\n"  # 2
    "Intro line.\n"  # 3
    "More intro.\n"  # 4
    "\n"  # 5
    "The service listens on port 8113.\n"  # 6
    "Health is on /health.\n"  # 7
    "\n"  # 8
    "Run `magickit serve` to start.\n"  # 9
    "\n"  # 10
    "Footer one.\n"  # 11
    "Footer two.\n"  # 12
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "verimend.sqlite3"
    migrate(db)
    with connection(db) as c:
        yield c


class Recording(FakeClient):
    def __init__(self, *script: Any) -> None:
        super().__init__(*script)
        self.tiers: list[str] = []

    def complete(self, messages, tier):
        self.tiers.append(tier)
        return super().complete(messages, tier)


def _run(conn: sqlite3.Connection, text: str = TEXT, path: str = PATH) -> int:
    run = conn.execute(
        "INSERT INTO crawl_run (started_at, status, stats_json) VALUES ('2026-10-28T00:00:00Z', 'running', '{}')"
    ).lastrowid
    _document(conn, run, text, path)
    return run


def _document(conn: sqlite3.Connection, run: int, text: str, path: str = PATH) -> None:
    conn.execute(
        "INSERT INTO document (run_id, doc_repo, doc_path, content_hash, extractor_key, chunk_hash, status)"
        " VALUES (?, ?, ?, ?, 'k', 'c', 'extracted')",
        (run, REPO, path, hashlib.sha256(text.encode("utf-8")).hexdigest()),
    )


def _fact(conn: sqlite3.Connection, run: int, content: dict[str, Any] | None = None) -> int:
    content = content or {"extractor": "ports", "port": 8120, "service": "magickit"}
    return conn.execute(
        "INSERT INTO fact (run_id, source_kind, source_ref, content, content_hash) VALUES (?, 'config', ?, ?, 'h')",
        (run, f"{REPO}@abc:src/config.py:4", canonical_json(content)),
    ).lastrowid


def _stale(
    conn: sqlite3.Connection,
    run: int,
    anchor: tuple[int | None, int | None] = (6, 6),
    evidence: Any = None,
    *,
    fact_ids: list[int] | None = None,
    method: str = "llm",
    path: str = PATH,
    result: str = "stale",
) -> int:
    claim = conn.execute(
        "INSERT INTO claim (run_id, doc_repo, doc_path, anchor_start, anchor_end, text, claim_type)"
        " VALUES (?, ?, ?, ?, ?, 'The service listens on port 8113.', 'port')",
        (run, REPO, path, *anchor),
    ).lastrowid
    if evidence is None:
        evidence = (
            {"judge": "llm", "tier": "light", "fact_ids": fact_ids, "reason": "the config says 8120"}
            if method == "llm"
            else {"checker": "port", "fact_ids": fact_ids, "reason": "port_mismatch"}
        )
    evidence_json = evidence if isinstance(evidence, str) else json.dumps(evidence)
    return conn.execute(
        "INSERT INTO verdict (claim_id, result, method, evidence_json) VALUES (?, ?, ?, ?)",
        (claim, result, method, evidence_json),
    ).lastrowid


def _mender(client: Any, **kwargs: Any) -> MenderSession:
    kwargs.setdefault("prompt", "mender prompt")
    return MenderSession(client, sleep=lambda s: None, **kwargs)


def _docs(text: str = TEXT, path: str = PATH) -> list[Document]:
    return [Document(REPO, path, text)]


def lines(*items: str) -> str:
    return json.dumps({"lines": list(items)})


def _draft(conn: sqlite3.Connection, verdict: int) -> str | None:
    return conn.execute("SELECT patch_draft FROM verdict WHERE id = ?", (verdict,)).fetchone()[0]


def _mend_stats(conn: sqlite3.Connection, run: int) -> dict[str, Any]:
    (stats_json,) = conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run,)).fetchone()
    return json.loads(stats_json)["mend"]


def apply_patch(text: str, patch: str) -> str:
    """A minimal unified-diff applier for the tests: checks every context and removed line."""
    old = text.splitlines(keepends=True)
    out: list[str] = []
    pos = 0
    body = patch.splitlines(keepends=True)[2:]
    i = 0
    while i < len(body):
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", body[i])
        assert m, body[i]
        start = int(m.group(1)) - (0 if m.group(2) == "0" else 1)
        out += old[pos:start]
        pos = start
        i += 1
        while i < len(body) and not body[i].startswith("@@"):
            line = body[i]
            nxt = body[i + 1] if i + 1 < len(body) else ""
            no_eol = nxt.startswith("\\ No newline")
            content = line[1:-1] if no_eol else line[1:]
            if line[0] in " -":
                assert old[pos] == content, (old[pos], content)
                pos += 1
            if line[0] in " +":
                out.append(content)
            i += 2 if no_eol else 1
    out += old[pos:]
    return "".join(out)


def changed_old_lines(patch: str) -> tuple[set[int], int]:
    """The 1-based original line numbers the diff removes, and how many lines it adds."""
    removed: set[int] = set()
    added = 0
    n = 0
    for line in patch.splitlines()[2:]:
        m = re.match(r"@@ -(\d+)", line)
        if m:
            n = int(m.group(1))
        elif line.startswith("-"):
            removed.add(n)
            n += 1
        elif line.startswith("+"):
            added += 1
        elif line.startswith(" "):
            n += 1
    return removed, added


# ------------------------------------------------------------------ drafts


@pytest.mark.parametrize("method", ["llm", "deterministic"])
def test_a_stale_verdict_gets_a_draft_confined_to_its_anchor(conn, method) -> None:
    run = _run(conn)
    fact = _fact(conn, run)
    verdict = _stale(conn, run, (6, 7), fact_ids=[fact], method=method)
    client = Recording(lines("The service listens on port 8120.", "Health is on /health."))
    passed = mend_stale(conn, run, _docs(), _mender(client))

    patch = _draft(conn, verdict)
    assert passed.drafted == 1 and patch is not None
    assert client.tiers == ["heavy"]
    assert patch.startswith(f"--- a/{PATH}\n+++ b/{PATH}\n")
    removed, added = changed_old_lines(patch)
    assert removed == {6} and added == 1  # line 7 came back unchanged, so it is context
    assert all(6 <= n <= 7 for n in removed)
    assert apply_patch(TEXT, patch) == TEXT.replace("port 8113", "port 8120")
    stats = _mend_stats(conn, run)
    assert stats["drafted"] == 1 and stats["failed"] == [] and "aborted" not in stats


def test_the_prompt_carries_the_claim_reason_facts_and_marked_anchor(conn) -> None:
    run = _run(conn)
    fact = _fact(conn, run)
    _stale(conn, run, (6, 6), fact_ids=[fact])
    client = Recording(lines("The service listens on port 8120."))
    mend_stale(conn, run, _docs(), _mender(client))
    user = client.calls[0][1]["content"]
    assert "The service listens on port 8113." in user and "the config says 8120" in user
    assert f'"fact_id": {fact}' in user and '"port":8120' in user
    assert "EDIT    6 | The service listens on port 8113." in user
    assert "KEEP    3 | Intro line." in user and "KEEP    9 | Run `magickit serve` to start." in user
    assert "Intro line" not in user.split("Passage")[0]
    assert "KEEP    2 |" not in user and "KEEP   10 |" not in user  # three lines of context each side


def test_a_model_that_rewrites_beyond_the_anchor_still_cannot_change_outside_it(conn) -> None:
    run = _run(conn)
    fact = _fact(conn, run)
    verdict = _stale(conn, run, (6, 6), fact_ids=[fact])
    # The model echoes the context it was told not to touch, with an edit to it.
    reply = lines("Intro line CHANGED.", "The service listens on port 8120.", "Health is on /HEALTH.")
    mend_stale(conn, run, _docs(), _mender(FakeClient(reply)))
    patch = _draft(conn, verdict)
    removed, _ = changed_old_lines(patch)
    assert removed == {6}
    patched = apply_patch(TEXT, patch).splitlines(keepends=True)
    original = TEXT.splitlines(keepends=True)
    assert patched[:5] == original[:5] and patched[-6:] == original[-6:]


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (lines(), "empty"),
        (lines("", "   "), "empty"),
        (lines("The service listens on port 8113."), "no_change"),
        (lines(*[f"line {i}" for i in range(11)]), "runaway"),
    ],
)
def test_rejected_drafts_are_not_written(conn, reply, reason) -> None:
    run = _run(conn)
    fact = _fact(conn, run)
    verdict = _stale(conn, run, (6, 6), fact_ids=[fact])
    passed = mend_stale(conn, run, _docs(), _mender(FakeClient(reply)))
    assert _draft(conn, verdict) is None and passed.rejected[reason] == 1
    stats = _mend_stats(conn, run)
    assert stats["rejected"][reason] == 1 and stats["drafted"] == 0
    assert "failed" in stats and stats["failed"] == []  # a rejection is not a stage failure


def test_runaway_limit_is_three_times_the_anchor_or_ten_lines() -> None:
    assert check_replacement(["a"], ["b"] * 10) is None
    assert check_replacement(["a"], ["b"] * 11) == "runaway"
    assert check_replacement(["a"] * 5, ["b"] * 15) is None
    assert check_replacement(["a"] * 5, ["b"] * 16) == "runaway"


def test_a_rerun_does_not_redraft(conn) -> None:
    run = _run(conn)
    fact = _fact(conn, run)
    verdict = _stale(conn, run, (6, 6), fact_ids=[fact])
    mend_stale(conn, run, _docs(), _mender(FakeClient(lines("The service listens on port 8120."))))
    first = _draft(conn, verdict)
    again = FakeClient()  # any call would fail the test
    passed = mend_stale(conn, run, _docs(), _mender(again))
    assert again.calls == [] and passed.drafted == 0
    assert _draft(conn, verdict) == first and _mend_stats(conn, run)["drafted"] == 1


def test_only_stale_verdicts_are_drafted(conn) -> None:
    run = _run(conn)
    fact = _fact(conn, run)
    _stale(conn, run, (6, 6), fact_ids=[fact], result="verified")
    _stale(conn, run, (7, 7), {"judge": "llm", "reason": "no_candidate_facts"}, result="unverifiable")
    client = FakeClient()
    mend_stale(conn, run, _docs(), _mender(client))
    assert client.calls == []


# ----------------------------------------------------------------- skips


@pytest.mark.parametrize(
    "case", ["json_null", "fact_ids_empty", "fact_ids_missing", "not_json", "fact_of_another_run", "fact_unknown"]
)
def test_a_stale_without_usable_evidence_is_skipped_and_the_rest_drafted(conn, case, caplog) -> None:
    run = _run(conn)
    fact = _fact(conn, run)
    other_run = conn.execute(
        "INSERT INTO crawl_run (started_at, status, stats_json) VALUES ('x', 'succeeded', '{}')"
    ).lastrowid
    foreign = _fact(conn, other_run)
    evidence: Any = {
        "json_null": "null",
        "fact_ids_empty": {"checker": "port", "fact_ids": [], "reason": "r"},
        "fact_ids_missing": {"checker": "port", "reason": "r"},
        "not_json": "{oops",
        "fact_of_another_run": {"checker": "port", "fact_ids": [fact, foreign], "reason": "r"},
        "fact_unknown": {"checker": "port", "fact_ids": [fact, 999_999], "reason": "r"},
    }[case]
    bad = _stale(conn, run, (6, 6), evidence)
    good = _stale(conn, run, (9, 9), fact_ids=[fact])
    client = Recording(lines("Run `magickit start` to start."))
    with caplog.at_level("ERROR"):
        passed = mend_stale(conn, run, _docs(), _mender(client))
    assert len(client.calls) == 1 and "magickit serve" in client.calls[0][1]["content"]
    assert passed.skipped["no_evidence"] == 1 and _mend_stats(conn, run)["skipped"]["no_evidence"] == 1
    assert _draft(conn, bad) is None and _draft(conn, good) is not None
    assert any(f"verdict {bad}:" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


def test_null_evidence_json_is_no_evidence() -> None:
    # verdict.evidence_json is NOT NULL in the schema, so a NULL cannot reach the stage from the table;
    # the check still holds for it.
    assert _evidence(None) is None


@pytest.mark.parametrize("case", ["edited", "missing_text", "missing_row"])
def test_a_document_that_is_not_the_extracted_text_is_skipped(conn, case) -> None:
    run = _run(conn)
    fact = _fact(conn, run)
    path = PATH
    docs = _docs()
    if case == "edited":
        docs = _docs(TEXT + "new line\n")
    elif case == "missing_text":
        docs = []
    else:
        path = "docs/other.md"
        docs = _docs(path=path)
    verdict = _stale(conn, run, (6, 6), fact_ids=[fact], path=path)
    client = FakeClient()
    passed = mend_stale(conn, run, docs, _mender(client))
    assert client.calls == [] and _draft(conn, verdict) is None
    assert passed.skipped["doc_changed"] == 1 and _mend_stats(conn, run)["skipped"]["doc_changed"] == 1


@pytest.mark.parametrize("anchor", [(None, None), (6, 99), (0, 1)])
def test_a_stale_without_a_usable_anchor_is_skipped(conn, anchor) -> None:
    run = _run(conn)
    fact = _fact(conn, run)
    verdict = _stale(conn, run, anchor, fact_ids=[fact])
    client = FakeClient()
    passed = mend_stale(conn, run, _docs(), _mender(client))
    assert client.calls == [] and _draft(conn, verdict) is None and passed.skipped["no_anchor"] == 1


# --------------------------------------------------------- Lexora failures


def _three(conn) -> tuple[int, list[int]]:
    run = _run(conn)
    fact = _fact(conn, run)
    return run, [_stale(conn, run, (n, n), fact_ids=[fact]) for n in (6, 7, 9)]


def test_a_malformed_draft_is_retried_once_then_fails_the_verdict(conn) -> None:
    run, verdicts = _three(conn)
    client = FakeClient("not json", '{"lines": "x"}', lines("a"), lines("b"))
    passed = mend_stale(conn, run, _docs(), _mender(client))
    assert len(client.calls) == 4 and "rejected for these reasons" in client.calls[1][-1]["content"]
    assert _mend_stats(conn, run)["failed"] == [{"verdict_id": verdicts[0], "error": "schema"}]
    assert passed.drafted == 2


def test_a_config_error_stops_the_stage(conn) -> None:
    run, verdicts = _three(conn)
    client = FakeClient(http(404))
    passed = mend_stale(conn, run, _docs(), _mender(client))
    assert len(client.calls) == 1 and passed.not_sent == 3
    stats = _mend_stats(conn, run)
    assert stats["config_error"] == {"status": 404, "at_verdict": verdicts[0]}
    with pytest.raises(MenderConfigError):
        mend_stale(conn, run, _docs(), _mender(FakeClient(http(401)), strict=True))


def test_three_transient_verdicts_in_a_row_abort(conn) -> None:
    run, verdicts = _three(conn)
    client = FakeClient(*[transport()] * 6)
    mend_stale(conn, run, _docs(), _mender(client))
    aborted = _mend_stats(conn, run)["aborted"]
    assert aborted == {"reason": "consecutive_transient_failures", "at_verdict": verdicts[2]}


def test_strict_transient_abort_has_the_shape_and_chains_the_client_error(conn) -> None:
    run, verdicts = _three(conn)
    second = transport()
    with pytest.raises(MenderAborted) as info:
        mend_stale(conn, run, _docs(), _mender(FakeClient(transport(), second), strict=True))
    detail = info.value.detail
    assert set(detail) == {"reason", "at_verdict"} and detail == {"reason": "transport", "at_verdict": verdicts[0]}
    assert info.value.__cause__.__cause__ is second
    assert "cause" not in detail
    json.dumps(detail)


def test_strict_permanent_streak_abort_has_the_same_keys_and_chains_the_third_failure(conn) -> None:
    run, verdicts = _three(conn)
    third = http(400)
    with pytest.raises(MenderAborted) as info:
        mend_stale(conn, run, _docs(), _mender(FakeClient(http(400), http(400), third), strict=True))
    detail = info.value.detail
    assert set(detail) == {"reason", "at_verdict", "status"} and detail["at_verdict"] == verdicts[2]
    assert info.value.__cause__.__cause__ is third
    assert "cause" not in detail


def test_unwaitable_retry_after_aborts_at_once(conn) -> None:
    run, verdicts = _three(conn)
    client = FakeClient(http(429, "600"))
    mend_stale(conn, run, _docs(), _mender(client))
    assert _mend_stats(conn, run)["aborted"] == {
        "reason": "retry_after_exceeds_max", "at_verdict": verdicts[0], "retry_after_s": 600
    }
    assert len(client.calls) == 1


def test_a_non_strict_abort_record_carries_no_cause_and_logs_its_traceback(conn, caplog) -> None:
    run, verdicts = _three(conn)
    third = http(400)
    with caplog.at_level("ERROR"):
        passed = mend_stale(conn, run, _docs(), _mender(FakeClient(http(400), http(400), third)))
    aborted = _mend_stats(conn, run)["aborted"]
    assert set(aborted) == {"reason", "at_verdict", "status"} and aborted["status"] == 400
    assert len(passed.failures) == 3
    (record,) = [r for r in caplog.records if r.getMessage().startswith("mender aborted")]
    assert record.exc_info is not None and record.exc_info[1].__cause__ is third


def test_an_abort_with_no_cause_keeps_the_implicit_context(conn) -> None:
    session = _mender(FakeClient(), strict=True)
    with pytest.raises(MenderAborted) as info:
        try:
            raise ValueError("underneath")
        except ValueError:
            session._abort("consecutive_transient_failures", 1)
    assert info.value.__cause__ is None and isinstance(info.value.__context__, ValueError)


# ------------------------------------------- permanent schema streak (msg-203)

BAD = ("not json", '{"lines": "x"}')  # one verdict whose draft stays malformed after the correction
GOOD = lines("The service listens on port 8120.")


def _many(conn, n: int) -> tuple[int, list[int]]:
    run = _run(conn)
    fact = _fact(conn, run)
    return run, [_stale(conn, run, (a, a), fact_ids=[fact]) for a in (6, 7, 9, 11, 12)[:n]]


def test_three_schema_failures_in_a_row_abort_with_their_own_reason(conn) -> None:
    run, verdicts = _many(conn, 4)
    client = FakeClient(*BAD, *BAD, *BAD, GOOD)
    passed = mend_stale(conn, run, _docs(), _mender(client))
    assert len(client.calls) == 6 and passed.not_sent == 1  # verdict 4 is never sent
    stats = _mend_stats(conn, run)
    assert stats["aborted"] == {"reason": "permanent_schema_streak", "at_verdict": verdicts[2]}  # no status
    assert [f["error"] for f in stats["failed"]] == ["schema"] * 3


def test_strict_schema_streak_chains_the_third_failure(conn) -> None:
    from verimend.llm_policy import Permanent

    run, verdicts = _three(conn)
    with pytest.raises(MenderAborted) as info:
        mend_stale(conn, run, _docs(), _mender(FakeClient(*BAD, *BAD, *BAD), strict=True))
    assert info.value.detail == {"reason": "permanent_schema_streak", "at_verdict": verdicts[2]}
    assert isinstance(info.value.__cause__, Permanent) and info.value.__cause__.error == "schema"


def test_a_well_formed_draft_resets_the_schema_streak(conn) -> None:
    run, verdicts = _many(conn, 4)
    client = FakeClient(*BAD, GOOD, *BAD, *BAD)
    passed = mend_stale(conn, run, _docs(), _mender(client))
    assert "aborted" not in _mend_stats(conn, run) and passed.drafted == 1 and len(passed.failures) == 3


def test_a_rejected_but_well_formed_draft_also_resets_the_schema_streak(conn) -> None:
    run, verdicts = _many(conn, 4)
    client = FakeClient(*BAD, lines(), *BAD, *BAD)  # empty: rejected, yet the output followed the schema
    mend_stale(conn, run, _docs(), _mender(client))
    assert "aborted" not in _mend_stats(conn, run)


def test_an_http_4xx_does_not_reset_the_schema_streak(conn) -> None:
    run, verdicts = _many(conn, 5)
    client = FakeClient(*BAD, http(400), *BAD, *BAD)
    passed = mend_stale(conn, run, _docs(), _mender(client))
    stats = _mend_stats(conn, run)
    assert stats["aborted"] == {"reason": "permanent_schema_streak", "at_verdict": verdicts[3]}
    assert [f["error"] for f in stats["failed"]] == ["schema", "http_400", "schema", "schema"]
    assert passed.not_sent == 1


def test_a_schema_failure_resets_the_http_streak_and_does_not_abort_it(conn) -> None:
    run, verdicts = _many(conn, 4)
    client = FakeClient(http(400), *BAD, http(400), http(400))
    mend_stale(conn, run, _docs(), _mender(client))
    stats = _mend_stats(conn, run)
    assert "aborted" not in stats
    assert [f["error"] for f in stats["failed"]] == ["http_400", "schema", "http_400", "http_400"]


def test_drafts_written_before_an_abort_stay(conn) -> None:
    run, verdicts = _three(conn)
    client = FakeClient(lines("The service listens on port 8120."), http(429, "600"))
    mend_stale(conn, run, _docs(), _mender(client))
    assert _draft(conn, verdicts[0]) is not None and _draft(conn, verdicts[1]) is None
    assert _mend_stats(conn, run)["drafted"] == 1


# ------------------------------------------------------------ the diff itself


def test_a_file_without_a_final_newline_keeps_it() -> None:
    text = "a\nb\nlast"
    patch = build_patch("x.md", text.splitlines(keepends=True), (3, 3), ["LAST"])
    assert "\\ No newline at end of file" in patch
    assert apply_patch(text, patch) == "a\nb\nLAST"


def test_crlf_endings_are_kept() -> None:
    text = "a\r\nb\r\nc\r\n"
    patch = build_patch("x.md", text.splitlines(keepends=True), (2, 2), ["B1", "B2"])
    assert apply_patch(text, patch) == "a\r\nB1\r\nB2\r\nc\r\n"


def test_validate_draft_frames() -> None:
    assert validate_draft('```json\n{"lines": ["a"]}\n```') == (["a"], [])
    assert validate_draft('{"lines": ["a\\nb"]}') == (["a", "b"], [])
    assert validate_draft('{"lines": ["a"], "note": 1}')[0] is None
    assert validate_draft('{"lines": [1]}')[0] is None
    assert validate_draft("[]")[0] is None
    assert validate_draft(None)[0] is None


def test_mender_tier_defaults_to_heavy_and_rejects_empty(monkeypatch) -> None:
    monkeypatch.delenv("VERIMEND_MENDER_TIER", raising=False)
    assert Settings().mender_tier == "heavy"
    monkeypatch.setenv("VERIMEND_MENDER_TIER", " ")
    with pytest.raises(ValueError, match="mender_tier"):
        Settings()


# ---------------------------------------- groups on one anchor (run 4, msg-269)


def test_verdicts_on_the_same_anchor_are_one_call_and_one_draft(conn) -> None:
    """Fixture (a): run 4's shape -- one verdict per parameter, all on one line."""
    run = _run(conn)
    fact = _fact(conn, run)
    verdicts = [_stale(conn, run, (6, 6), fact_ids=[fact]) for _ in range(3)]
    client = Recording(lines("The service listens on port 8120."))
    passed = mend_stale(conn, run, _docs(), _mender(client))
    assert client.tiers == ["heavy"] and passed.drafted == 3
    patches = {_draft(conn, v) for v in verdicts}
    assert len(patches) == 1 and None not in patches
    user = client.calls[0][1]["content"]
    assert "3 stale claims from README.md, all about lines 6-6" in user
    assert user.count(f'"fact_id": {fact}') == 1  # shared evidence is sent once
    assert _mend_stats(conn, run)["drafted"] == 3


def test_overlapping_but_unequal_anchors_are_not_grouped(conn) -> None:
    """Fixture (a'): only an exactly equal anchor groups; overlaps are never chained."""
    run = _run(conn)
    fact = _fact(conn, run)
    wide = _stale(conn, run, (6, 7), fact_ids=[fact])
    narrow = _stale(conn, run, (7, 7), fact_ids=[fact])
    client = Recording(
        lines("The service listens on port 8120.", "Health is on /health."), lines("Health is on /healthz.")
    )
    passed = mend_stale(conn, run, _docs(), _mender(client))
    assert len(client.tiers) == 2 and passed.drafted == 2
    assert _draft(conn, wide) != _draft(conn, narrow)


def test_a_rejected_group_counts_every_verdict_and_lists_structure_ids(conn) -> None:
    text = TEXT.replace("The service listens on port 8113.", "> The service listens on port 8113.")
    run = _run(conn, text)
    fact = _fact(conn, run)
    verdicts = [_stale(conn, run, (6, 6), fact_ids=[fact]) for _ in range(2)]
    passed = mend_stale(conn, run, _docs(text), _mender(FakeClient(lines("The service listens on port 8120."))))
    assert passed.rejected["structure"] == 2 and passed.drafted == 0
    stats = _mend_stats(conn, run)
    assert stats["rejected"]["structure"] == 2 and stats["rejected_structure"] == verdicts
    assert all(_draft(conn, v) is None for v in verdicts)


def test_a_failed_group_fails_each_of_its_verdicts(conn) -> None:
    run = _run(conn)
    fact = _fact(conn, run)
    verdicts = [_stale(conn, run, (6, 6), fact_ids=[fact]) for _ in range(2)]
    mend_stale(conn, run, _docs(), _mender(FakeClient(http(400), http(400))))
    assert [f["verdict_id"] for f in _mend_stats(conn, run)["failed"]] == verdicts


# --------------------------------------------- structure rules (msg-271..277)


def _structure(text: str, anchor: tuple[int, int], replacement: list[str]) -> str | None:
    from verimend.mender.draft import check_structure

    return check_structure(build_patch("x.md", text.splitlines(keepends=True), anchor, replacement))


QUOTE = (
    "Intro.\n"
    "\n"
    "> **Rule**\n"
    ">\n"
    "> `checkpoint` and `handoff` may return `success: false`:\n"  # 5
    ">\n"
    "> 1. **Do not hand over.**\n"
    "> 2. **Do not retry.**\n"
    "\n"
    "After.\n"
)


def test_dropping_a_blockquote_mark_is_rule1() -> None:
    """Fixture (b): the rewritten line loses its ``> ``."""
    assert _structure(QUOTE, (5, 5), ["`checkpoint` may return `success: false`:"]) == "rule1"


def test_run4_519_shape_unquoted_copies_of_kept_lines_are_rejected() -> None:
    """Fixture (b'): verdict 519 -- the quoted items stay as context and unquoted copies are added."""
    replacement = [
        "> `checkpoint` may return `success: false`:",
        "",
        "1. **Do not hand over.**",
        "2. **Do not retry.**",
    ]
    assert _structure(QUOTE, (5, 5), replacement) == "rule3"


def test_changing_a_heading_level_or_list_kind_is_rule1() -> None:
    text = "## Title\n\n- item one\n"
    assert _structure(text, (1, 1), ["### Title"]) == "rule1"
    assert _structure(text, (3, 3), ["1. item one"]) == "rule1"
    assert _structure(text, (3, 3), ["- item 1"]) is None


TABLE = (
    "| action | parameters |\n"
    "|---|---|\n"
    "| `search` | query, limit |\n"  # 3
    "| `delete` | doc_id |\n"
    r"| `split` | a \| b |" "\n"  # 5: an escaped pipe in the cell
    r"| `path` | C:\\|" "\n"  # 6: the cell ends with an escaped backslash, right before the column pipe
)


def test_a_table_row_with_an_extra_column_is_rule2() -> None:
    """Fixture (b): verdict 1861 -- the row gains an empty first cell."""
    assert _structure(TABLE, (4, 4), ["| | `delete` | doc_id, permanent |"]) == "rule2"
    assert _structure(TABLE, (3, 3), ["| `search` | query, search_limit |"]) is None


def test_escaped_pipes_in_a_cell_are_not_columns() -> None:
    r"""Fixture (b''): adding or removing ``\|`` inside a cell keeps the columns."""
    assert _structure(TABLE, (5, 5), [r"| `split` | a \| b \| c |"]) is None
    assert _structure(TABLE, (5, 5), ["| `split` | a or b |"]) is None
    assert _structure(TABLE, (5, 5), ["| `split` | a | b |"]) == "rule2"


def test_a_cell_ending_in_an_escaped_backslash_keeps_its_pipe_a_column() -> None:
    r"""Fixture (b''''): ``\\|`` is an escaped backslash, then a column; ``\\\|`` ends in a text pipe."""
    from verimend.mender.draft import _columns

    assert _columns(r"| `path` | C:\\|") == 3 and _columns(r"| `path` | C:\\\|") == 2
    assert _structure(TABLE, (6, 6), [r"| `path` | D:\\|"]) is None
    assert _structure(TABLE, (6, 6), [r"| `path` | D:\\\|"]) == "rule2"


def test_blank_lines_and_rules_may_repeat_context() -> None:
    """Fixture (b'''): a blank line or ``---`` equal to a context line is not a duplicate."""
    text = "Para one.\n\nPara two old.\n\n---\n\nEnd.\n"
    assert _structure(text, (3, 3), ["Para two new.", "", "Para three."]) is None
    assert _structure(text, (3, 3), ["Para two new.", "---", "More."]) is None


CODE = (
    "Example:\n"
    "\n"
    "```json\n"
    "{\n"
    '  "a": {\n'
    '    "x": 1\n'  # 6
    "  },\n"
    '  "b": 2\n'
    "}\n"
    "```\n"  # 10
    "\n"
    "Done.\n"
)


def test_adding_an_entry_with_a_closing_brace_is_kept() -> None:
    """Fixture (d): a ``},`` line equal to a context line is not a duplicate (rule 3 needs text)."""
    assert _structure(CODE, (6, 6), ['    "x": 1', "  },", '  "c": {', '    "y": 3']) is None


def test_dropping_a_closing_fence_is_rule4() -> None:
    """Fixture (e)."""
    assert _structure(CODE, (9, 10), ["}"]) == "rule4"


def test_adding_or_removing_a_whole_code_block_is_kept() -> None:
    """Fixture (f): an even change in fences."""
    text = "Run it:\n\n```sh\nold-cmd\n```\n\nDone.\n"
    assert _structure(text, (3, 5), ["Run `new-cmd`."]) is None
    assert _structure(text, (7, 7), ["Done, or:", "", "```sh", "other-cmd", "```"]) is None


# ---------------------------------------- line marks that are not Markdown (msg-284/286/288)


def _passage(user: str) -> list[str]:
    return user.split("Passage (rewrite only the lines marked EDIT):", 1)[1].splitlines()[1:]


def test_a_blockquote_line_keeps_its_own_mark_after_the_edit_mark(conn) -> None:
    """Fixture (g): the quote's `>` is the line's text, and no line starts with a `>` mark."""
    text = TEXT.replace("The service listens on port 8113.", "> The service listens on port 8113.")
    run = _run(conn, text)
    fact = _fact(conn, run)
    _stale(conn, run, (6, 6), fact_ids=[fact])
    client = Recording(lines("> The service listens on port 8120."))
    passed = mend_stale(conn, run, _docs(text), _mender(client))
    user = client.calls[0][1]["content"]
    assert "EDIT    6 | > The service listens on port 8113." in user
    passage = _passage(user)
    assert passage and all(re.match(r"(EDIT|KEEP) {1,4}\d+ \| ", line) for line in passage)
    assert not any(line.startswith(">") for line in passage)
    assert passed.drafted == 1  # the quote was kept, so the structure rules let it through


def test_a_grouped_prompt_uses_the_same_marks(conn) -> None:
    """Fixture (h): the group prompt marks its passage the same way."""
    run = _run(conn)
    fact = _fact(conn, run)
    for _ in range(2):
        _stale(conn, run, (6, 7), fact_ids=[fact])
    client = Recording(lines("The service listens on port 8120.", "Health is on /health."))
    mend_stale(conn, run, _docs(), _mender(client))
    user = client.calls[0][1]["content"]
    assert "2 stale claims from README.md" in user
    passage = _passage(user)
    assert [line[:4] for line in passage] == ["KEEP"] * 3 + ["EDIT"] * 2 + ["KEEP"] * 3
    assert not any(line.startswith(">") for line in passage)


def test_the_prompt_says_to_keep_every_leading_mark_without_listing_them() -> None:
    """Fixture (g'): one general sentence, no enumerated symbols, no rewrite exception."""
    prompt = load_prompt()
    assert (
        "Each passage line's text starts right after the first `| `; everything after it is the"
        " document's own content. When you keep or rewrite a line, keep its leading whitespace,"
        " Markdown syntax and punctuation exactly as they are, even if you change the words after them."
    ) in " ".join(prompt.split())
    assert "unless the facts contradict it" not in prompt
    assert not re.search(r"`[>#*+-]`\s*[,、]\s*`[>#*+-]`", prompt)  # no list of symbols
    assert "marked `>`" not in prompt and "`>` marker" not in prompt
