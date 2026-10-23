"""Fact retrieval for the reconciler's LLM judge (docs/design.md 5.3): BM25 over a claim's facts.

``search(query, facts)`` ranks the facts with rank-bm25 and keeps the top
five whose score is above 0. Both sides go through
``verimend.reconciler.tokenize``: the query is the claim text plus the
subject's string values (``query_tokens``), a fact is the values of its
content (``fact_tokens``). An empty result means the judge has nothing to
judge with; the caller settles the claim without calling the LLM.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rank_bm25 import BM25Okapi

from verimend.reconciler.tokenize import tokenize

TOP_K = 5


@dataclass(frozen=True)
class FactDoc:
    id: int
    kind: str
    content: dict[str, Any]


class _BM25(BM25Okapi):
    """BM25Okapi with the non-negative idf ``log(1 + (N - n + 0.5) / (n + 0.5))`` (Lucene's).

    rank-bm25's own idf is negative for a term in more than half of the
    documents and floors it at ``epsilon * average_idf`` -- which is itself
    negative when the corpus is one or two facts, so a fact that *does* match
    the query would score below 0 and never become a candidate. With this idf
    a score is above 0 exactly when the fact shares a token with the query.
    """

    def _calc_idf(self, nd: dict[str, int]) -> None:
        for word, freq in nd.items():
            self.idf[word] = math.log(1 + (self.corpus_size - freq + 0.5) / (freq + 0.5))
        self.average_idf = sum(self.idf.values()) / len(self.idf) if self.idf else 0.0


def fact_tokens(content: Any) -> list[str]:
    """Tokens of a fact's *values* (strings and numbers, recursively). Keys are not indexed.

    Every ``ports`` fact has a ``port`` key; indexing keys would make "port"
    in a claim match all of them.
    """
    out: list[str] = []
    if isinstance(content, dict):
        for value in content.values():
            out.extend(fact_tokens(value))
    elif isinstance(content, list):
        for value in content:
            out.extend(fact_tokens(value))
    elif isinstance(content, str):
        out.extend(tokenize(content))
    elif isinstance(content, (int, float)) and not isinstance(content, bool):
        out.extend(tokenize(str(content)))
    return out


def query_tokens(text: str, subject_json: str | None) -> list[str]:
    """The claim text plus the string values of its subject, if it has one."""
    parts = [text]
    if subject_json:
        try:
            subject = json.loads(subject_json)
        except json.JSONDecodeError:
            subject = None
        if isinstance(subject, dict):
            parts.extend(v for v in subject.values() if isinstance(v, str))
    return tokenize(" ".join(parts))


def search(query: Sequence[str], facts: Sequence[FactDoc], k: int = TOP_K) -> list[FactDoc]:
    """The top ``k`` facts by BM25 score, score > 0 only; ties keep fact order."""
    if not query or not facts:
        return []
    corpus = [fact_tokens(f.content) for f in facts]
    if not any(corpus):
        return []
    scores = _BM25(corpus).get_scores(list(query))
    ranked = sorted((i for i in range(len(facts)) if scores[i] > 0), key=lambda i: (-scores[i], i))
    return [facts[i] for i in ranked[:k]]
