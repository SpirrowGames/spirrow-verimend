"""BM25 retrieval for the LLM judge (verimend/reconciler/retrieval.py, design 5.3)."""

from __future__ import annotations

import json

from verimend.reconciler.retrieval import FactDoc, query_tokens, search


def _docs(*contents: dict) -> list[FactDoc]:
    return [FactDoc(i + 1, "file", c) for i, c in enumerate(contents)]


def test_bm25_keeps_at_most_five_and_ranks_the_best_first() -> None:
    facts = _docs(
        *[{"extractor": "ports", "port": 9000 + i} for i in range(7)],
        {"extractor": "ports", "port": 8113, "via": "rest api"},
    )
    found = search(query_tokens("The REST API listens on port 8113.", None), facts)
    assert found[0].id == 8
    assert len(found) == 1  # "ports"/"port" are a value and a key: no other fact shares a token


def test_bm25_top_five_when_more_match() -> None:
    facts = _docs(*[{"tool": "chatroom_post_message", "param": f"p{i}"} for i in range(8)])
    found = search(query_tokens("chatroom_post_message takes a body", None), facts)
    assert [f.id for f in found] == [1, 2, 3, 4, 5]


def test_bm25_scores_a_single_matching_fact_above_zero() -> None:
    # rank-bm25's own idf goes negative for a one-document corpus; ours does not.
    assert [f.id for f in search(["8113"], _docs({"port": 8113}))] == [1]


def test_bm25_keys_are_not_indexed() -> None:
    assert search(query_tokens("the port is configurable", None), _docs({"port": 8113})) == []


def test_query_includes_the_subject_string_values() -> None:
    assert "chatroom_get_thread" in query_tokens("a tool", json.dumps({"polarity": "current", "tool": "chatroom_get_thread", "param": "x"}))


def test_japanese_claim_finds_a_japanese_fact() -> None:
    facts = _docs({"extractor": "service_health", "service": "lexora", "status": "稼働中"}, {"note": "無関係"})
    found = search(query_tokens("Lexora は稼働中です", None), facts)
    assert [f.id for f in found] == [1]


def test_no_query_no_facts_or_facts_without_values_find_nothing() -> None:
    assert search([], _docs({"port": 8113})) == []
    assert search(["8113"], []) == []
    assert search(["8113"], _docs({}, {"x": None})) == []
