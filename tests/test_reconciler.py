"""The deterministic reconciler (verimend/reconciler, docs/design.md 5.3).

Claims are inserted directly: the extractor that writes them does not exist
yet. Every claim carries the structured subject the extractor will produce.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from verimend.collector.facts import canonical_json
from verimend.db import connection, migrate
from verimend.reconciler import reconcile

REPO = "SpirrowGames/spirrow-magickit"
OTHER = "SpirrowGames/spirrow-voxelworld"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "verimend.sqlite3"
    migrate(db)
    with connection(db) as c:
        yield c


def _run(conn: sqlite3.Connection, stats: dict | None = None) -> int:
    return conn.execute(
        "INSERT INTO crawl_run (started_at, status, stats_json) VALUES ('2026-10-21T00:00:00Z', 'succeeded', ?)",
        (canonical_json(stats) if stats is not None else None,),
    ).lastrowid


def _fact(conn: sqlite3.Connection, run_id: int, content: dict[str, Any], repo: str = REPO, kind: str = "file") -> int:
    return conn.execute(
        "INSERT INTO fact (run_id, source_kind, source_ref, content, content_hash) VALUES (?, ?, ?, ?, 'h')",
        (run_id, kind, f"{repo}@abc123:src/x.py:1", canonical_json(content)),
    ).lastrowid


def _claim(
    conn: sqlite3.Connection,
    run_id: int,
    claim_type: str,
    subject: dict | str | None,
    text: str = "a claim",
    repo: str = REPO,
) -> int:
    subject_json = subject if isinstance(subject, str) or subject is None else json.dumps(subject)
    return conn.execute(
        "INSERT INTO claim (run_id, doc_repo, doc_path, anchor_start, anchor_end, text, claim_type, subject_json)"
        " VALUES (?, ?, 'README.md', 1, 1, ?, ?, ?)",
        (run_id, repo, text, claim_type, subject_json),
    ).lastrowid


def _verdict(conn: sqlite3.Connection, claim_id: int) -> tuple[str, str, dict] | None:
    row = conn.execute("SELECT result, method, evidence_json FROM verdict WHERE claim_id = ?", (claim_id,)).fetchone()
    return None if row is None else (row[0], row[1], json.loads(row[2]))


def _port(port: int, **extra) -> dict:
    return {"extractor": "ports", "port": port, "via": "assign", "context": f"port = {port}", **extra}


def _tool(tool: str, params: list[str] | None, open_parameters: bool | None = False, **extra) -> dict:
    return {
        "extractor": "mcp_schema",
        "tool": tool,
        "function": tool,
        "parameters": None
        if params is None
        else [{"name": p, "annotation": "str", "default": None, "required": True} for p in params],
        "open_parameters": open_parameters,
        **extra,
    }


def _current(**fields) -> dict:
    return {"polarity": "current", **fields}


def _nothing_decided(considered: int) -> dict:
    return {"considered": considered, "verified": 0, "stale": 0, "deferred": considered, "invalid_subject": 0}


# --- held back: no subject, or a subject that is not about the present ------------------


def test_null_subject_is_deferred_even_when_the_text_names_a_matching_port(conn) -> None:
    run = _run(conn)
    _fact(conn, run, _port(8080))
    claim = _claim(conn, run, "port", None, text="Do not use port 8080")

    assert reconcile(conn, run).stats == _nothing_decided(1)
    assert _verdict(conn, claim) is None


@pytest.mark.parametrize("polarity", ["negated", "historical"])
def test_non_current_polarity_is_deferred(conn, polarity) -> None:
    run = _run(conn)
    _fact(conn, run, _port(8080))
    claim = _claim(conn, run, "port", {"polarity": polarity, "port": 8080})

    assert reconcile(conn, run).stats == _nothing_decided(1)
    assert _verdict(conn, claim) is None


def test_changed_from_to_is_judged_on_the_current_value_only(conn) -> None:
    """A "changed from 8080 to 9000" sentence is two claims; only 9000 is compared."""
    run = _run(conn)
    _fact(conn, run, _port(8080))
    old_claim = _claim(conn, run, "port", {"polarity": "historical", "port": 8080})
    new_claim = _claim(conn, run, "port", _current(port=9000))

    stats = reconcile(conn, run).stats

    assert _verdict(conn, old_claim) is None  # the 8080 fact verifies nothing
    assert _verdict(conn, new_claim) is None  # 9000 is not in the facts: undecided, never stale
    assert stats == _nothing_decided(2)


def test_types_without_a_checker_are_deferred(conn) -> None:
    run = _run(conn)
    claim = _claim(conn, run, "behavior", {"polarity": "current", "whatever": 1})
    assert reconcile(conn, run).stats == _nothing_decided(1)
    assert _verdict(conn, claim) is None


# --- invalid subjects: settled as unverifiable, never deferred, never fatal -------------


@pytest.mark.parametrize(
    "subject",
    [
        "{broken json",
        {"polarity": "current", "port": "8080"},  # a string is not a port
        {"polarity": "current", "port": 8080, "extra": True},
    ],
)
def test_invalid_subject_is_settled_unverifiable(conn, subject, caplog) -> None:
    run = _run(conn)
    _fact(conn, run, _port(8080))
    claim = _claim(conn, run, "port", subject)

    with caplog.at_level(logging.ERROR, logger="verimend.reconciler.run"):
        stats = reconcile(conn, run).stats

    result, method, evidence = _verdict(conn, claim)
    assert (result, method) == ("unverifiable", "deterministic")
    assert evidence["checker"] == "port"
    assert evidence["error"] == "subject_schema_violation"
    assert evidence["detail"]
    assert stats["invalid_subject"] == 1
    assert stats["deferred"] == 0
    assert any("invalid subject" in r.getMessage() for r in caplog.records)


def test_an_invalid_subject_does_not_affect_other_claims_or_the_run(conn) -> None:
    run = _run(conn, {"targets": {}})
    fact = _fact(conn, run, _port(8118))
    bad = _claim(conn, run, "port", {"polarity": "current", "port": "x"})
    good = _claim(conn, run, "port", _current(port=8118))
    pending = _claim(conn, run, "port", None)

    stats = reconcile(conn, run).stats

    assert _verdict(conn, bad)[0] == "unverifiable"
    assert _verdict(conn, good)[0] == "verified"
    assert _verdict(conn, good)[2]["fact_ids"] == [fact]
    assert _verdict(conn, pending) is None
    assert stats == {"considered": 3, "verified": 1, "stale": 0, "deferred": 1, "invalid_subject": 1}

    status, stats_json = conn.execute("SELECT status, stats_json FROM crawl_run WHERE id = ?", (run,)).fetchone()
    assert status == "succeeded"
    recorded = json.loads(stats_json)
    assert recorded["targets"] == {}  # the collector's stats are kept
    assert recorded["reconcile"] == {
        "verified": 1,
        "stale": 0,
        "unverifiable": 1,
        "deferred": 1,
        "invalid_subject": 1,
    }


# --- port -------------------------------------------------------------------------------


def test_port_verified_by_literal(conn) -> None:
    run = _run(conn)
    a = _fact(conn, run, _port(8118))
    b = _fact(conn, run, _port(8118, via="url"))
    _fact(conn, run, _port(8004))
    claim = _claim(conn, run, "port", _current(port=8118, service="verimend"))

    reconcile(conn, run)

    result, method, evidence = _verdict(conn, claim)
    assert (result, method) == ("verified", "deterministic")
    assert evidence["fact_ids"] == [a, b]
    assert evidence["match"] == "port_literal"
    assert evidence["checker"] == "port"
    assert evidence["reason"]


def test_missing_port_is_undecided_not_stale(conn) -> None:
    run = _run(conn)
    _fact(conn, run, _port(8004))
    claim = _claim(conn, run, "port", _current(port=8118))
    assert reconcile(conn, run).stats == _nothing_decided(1)
    assert _verdict(conn, claim) is None


# --- parameter (tool) ---------------------------------------------------------------------


def test_tool_parameter_verified(conn) -> None:
    run = _run(conn)
    tool = _fact(conn, run, _tool("research", ["query", "max_tokens"]), kind="tool_schema")
    claim = _claim(conn, run, "parameter", _current(tool="research", param="query"))

    reconcile(conn, run)

    result, _, evidence = _verdict(conn, claim)
    assert result == "verified"
    assert evidence["fact_ids"] == [tool]


def test_tool_parameter_stale_on_a_closed_single_definition(conn) -> None:
    run = _run(conn)
    tool = _fact(conn, run, _tool("research", ["query"]), kind="tool_schema")
    claim = _claim(conn, run, "parameter", _current(tool="research", param="top_k"))

    stats = reconcile(conn, run).stats

    result, method, evidence = _verdict(conn, claim)
    assert (result, method) == ("stale", "deterministic")
    assert evidence["fact_ids"] == [tool]  # stale always names the contradicting fact
    assert evidence["checker"] == "parameter"
    assert stats["stale"] == 1


_PREDATES_KEY = {k: v for k, v in _tool("research", ["query"]).items() if k != "open_parameters"}


@pytest.mark.parametrize(
    ("facts", "why"),
    [
        ([], "no fact for the tool"),
        ([_tool("research", ["query"], open_parameters=True)], "**kwargs / *args"),
        ([_tool("research", ["query"], open_parameters=None)], "open_parameters unknown"),
        ([_PREDATES_KEY], "fact predates the open_parameters key"),
        ([_tool("research", None, open_parameters=None)], "parameters is None"),
        ([_tool("research", ["query"]), _tool("research", ["query", "x"])], "two definitions"),
        ([_tool("research", ["query"], tool_expr="NAME")], "computed name"),
        ([_tool("other", ["top_k"])], "a different tool has the parameter"),
    ],
)
def test_tool_parameter_is_undecided_unless_the_signature_is_known_closed(conn, facts, why) -> None:
    run = _run(conn)
    for content in facts:
        _fact(conn, run, content, kind="tool_schema")
    claim = _claim(conn, run, "parameter", _current(tool="research", param="top_k"))

    assert reconcile(conn, run).stats == _nothing_decided(1), why
    assert _verdict(conn, claim) is None, why


def test_tool_parameter_verified_when_any_of_two_definitions_has_it(conn) -> None:
    run = _run(conn)
    _fact(conn, run, _tool("research", ["query"]), kind="tool_schema")
    second = _fact(conn, run, _tool("research", ["query", "top_k"]), kind="tool_schema")
    claim = _claim(conn, run, "parameter", _current(tool="research", param="top_k"))

    reconcile(conn, run)

    assert _verdict(conn, claim)[0] == "verified"
    assert _verdict(conn, claim)[2]["fact_ids"] == [second]


# --- parameter (env) ----------------------------------------------------------------------


def _env(env: str) -> dict:
    return {"extractor": "config_keys", "kind": "env_read", "env": env, "default": None, "required": False}


def test_env_verified(conn) -> None:
    run = _run(conn)
    fact = _fact(conn, run, _env("VERIMEND_DB_PATH"), kind="config")
    claim = _claim(conn, run, "parameter", _current(env="VERIMEND_DB_PATH"))

    reconcile(conn, run)

    assert _verdict(conn, claim)[0] == "verified"
    assert _verdict(conn, claim)[2]["fact_ids"] == [fact]


def test_missing_env_is_undecided_never_stale(conn) -> None:
    run = _run(conn)
    _fact(conn, run, _env("VERIMEND_DB_PATH"), kind="config")
    claim = _claim(conn, run, "parameter", _current(env="VERIMEND_GONE"))
    assert reconcile(conn, run).stats == _nothing_decided(1)
    assert _verdict(conn, claim) is None


# --- command ------------------------------------------------------------------------------


def _script(name: str) -> dict:
    return {"extractor": "entrypoints", "kind": "console_script", "name": name, "target": f"{name}.__main__:main"}


def _module(module: str) -> dict:
    return {"extractor": "entrypoints", "kind": "python_module", "module": module, "command": f"python -m {module}"}


@pytest.mark.parametrize(
    ("command", "match"),
    [
        ("verimend", "exact"),
        ("verimend   collect --repo o/r", "prefix"),
        ("python -m verimend", "exact"),
        ("python -m verimend migrate", "prefix"),
    ],
)
def test_command_verified(conn, command, match) -> None:
    run = _run(conn)
    _fact(conn, run, _script("verimend"), kind="config")
    _fact(conn, run, _module("verimend"), kind="config")
    claim = _claim(conn, run, "command", _current(command=command))

    reconcile(conn, run)

    result, _, evidence = _verdict(conn, claim)
    assert result == "verified"
    assert evidence["match"] == match


@pytest.mark.parametrize("command", ["verimend-cli", "python -m verimend.server", "uv run verimend"])
def test_unknown_command_is_undecided_never_stale(conn, command) -> None:
    run = _run(conn)
    _fact(conn, run, _script("verimend"), kind="config")
    _fact(conn, run, _module("verimend"), kind="config")
    claim = _claim(conn, run, "command", _current(command=command))

    assert reconcile(conn, run).stats == _nothing_decided(1)
    assert _verdict(conn, claim) is None


# --- scope: same run, same repository -----------------------------------------------------


def test_facts_of_another_repository_are_not_used(conn) -> None:
    run = _run(conn)
    _fact(conn, run, _port(8118), repo=OTHER)
    _fact(conn, run, _tool("research", ["query"]), repo=OTHER, kind="tool_schema")
    port_claim = _claim(conn, run, "port", _current(port=8118))
    param_claim = _claim(conn, run, "parameter", _current(tool="research", param="top_k"))

    assert reconcile(conn, run).stats == _nothing_decided(2)
    assert _verdict(conn, port_claim) is None
    assert _verdict(conn, param_claim) is None  # would be stale against the other repo's tool


def test_repository_prefix_is_not_a_like_pattern(conn) -> None:
    """``_`` in a name must not match any character (as it would under LIKE)."""
    run = _run(conn)
    _fact(conn, run, _port(8118), repo="o/aXb")
    claim = _claim(conn, run, "port", _current(port=8118), repo="o/a_b")
    reconcile(conn, run)
    assert _verdict(conn, claim) is None


def test_repository_prefix_does_not_match_a_longer_name(conn) -> None:
    run = _run(conn)
    _fact(conn, run, _port(8118), repo=REPO + "-extra")
    claim = _claim(conn, run, "port", _current(port=8118))
    reconcile(conn, run)
    assert _verdict(conn, claim) is None


def test_facts_of_another_run_are_not_used(conn) -> None:
    earlier = _run(conn)
    _fact(conn, earlier, _port(8118))
    run = _run(conn)
    claim = _claim(conn, run, "port", _current(port=8118))
    reconcile(conn, run)
    assert _verdict(conn, claim) is None


def test_claims_of_another_run_are_not_touched(conn) -> None:
    other = _run(conn)
    _fact(conn, other, _port(8118))
    untouched = _claim(conn, other, "port", _current(port=8118))
    run = _run(conn)

    assert reconcile(conn, run).stats["considered"] == 0
    assert _verdict(conn, untouched) is None


# --- re-runs ------------------------------------------------------------------------------


def test_rerun_does_not_duplicate_verdicts_and_keeps_totals(conn) -> None:
    run = _run(conn)
    _fact(conn, run, _port(8118))
    _claim(conn, run, "port", _current(port=8118))
    _claim(conn, run, "port", "{bad")
    _claim(conn, run, "port", None)

    first = reconcile(conn, run).stats
    second = reconcile(conn, run).stats

    assert first["considered"] == 3
    assert second == _nothing_decided(1)
    assert conn.execute("SELECT count(*) FROM verdict").fetchone()[0] == 2
    totals = json.loads(conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run,)).fetchone()[0])
    assert totals["reconcile"]["invalid_subject"] == 1  # not erased by the second pass
    assert totals["reconcile"]["verified"] == 1


def test_unknown_run_is_an_error(conn) -> None:
    with pytest.raises(ValueError):
        reconcile(conn, 999)
