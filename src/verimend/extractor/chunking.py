"""Deterministic chunking of a Markdown document (docs/design.md 5.2).

The extractor reads a document one chunk at a time, so the boundaries decide
what the LLM sees together. They are computed, never configured:

1. A chunk is a section: a heading line and everything up to the next heading.
   Text before the first heading is a section of its own. A ``#`` inside a
   fenced code block (```` ``` ```` / ``~~~``) is not a heading.
2. A section over ``MAX_CHUNK_TOKENS`` is split at paragraph boundaries (blank
   lines), packing whole paragraphs greedily.
3. A paragraph that is still over the limit is split at line boundaries, the
   same way.
4. A single line over the limit cannot be split further. It becomes a chunk of
   its own marked ``oversized``; the extractor fails it as ``oversized_line``
   without calling the LLM, since the request would only be rejected.

Line numbers are 1-based and absolute in the document, the same numbering the
LLM is asked to answer in. Chunks with nothing but blank lines are dropped.

``chunk_hash`` is the SHA-256 of the boundaries. It is part of the incremental
cache key, so a change to these rules that moves a boundary invalidates the
cache by itself, with no version string to remember to bump.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

MAX_CHUNK_TOKENS = 2000

_HEADING = re.compile(r"^ {0,3}#{1,6}(?:\s|$)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class Chunk:
    start: int  # first line, 1-based, inclusive
    end: int  # last line, inclusive
    lines: tuple[str, ...]
    oversized: bool = False

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)


def estimate_tokens(text: str) -> int:
    """A deterministic token estimate from the characters.

    ASCII runs about four characters per token; other scripts (Japanese
    documents are in scope) about one. Rounded up, so the estimate errs high.
    """
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other = len(text) - ascii_chars
    return -(-ascii_chars // 4) + other


def chunk_document(text: str, max_tokens: int = MAX_CHUNK_TOKENS) -> list[Chunk]:
    """Split ``text`` into chunks by the rules in the module docstring."""
    lines = text.splitlines()
    chunks: list[Chunk] = []
    for start, end in _sections(lines):
        chunks.extend(_fit(lines, start, end, max_tokens))
    return [c for c in chunks if any(line.strip() for line in c.lines)]


def chunk_hash(chunks: Sequence[Chunk]) -> str:
    """SHA-256 of the chunk boundaries (and which chunks are oversized)."""
    layout = [[c.start, c.end, c.oversized] for c in chunks]
    return hashlib.sha256(json.dumps(layout, separators=(",", ":")).encode("utf-8")).hexdigest()


def _sections(lines: Sequence[str]) -> list[tuple[int, int]]:
    """(start, end) 1-based inclusive line ranges, one per heading section."""
    starts = [1]
    fence: str | None = None
    for number, line in enumerate(lines, start=1):
        match = _FENCE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                continue
            if _HEADING.match(line) and number != 1:
                starts.append(number)
        elif match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
            fence = None
    if not lines:
        return []
    bounds = starts + [len(lines) + 1]
    return [(bounds[i], bounds[i + 1] - 1) for i in range(len(starts))]


def _tokens(lines: Sequence[str], start: int, end: int) -> int:
    return estimate_tokens("\n".join(lines[start - 1 : end]))


def _fit(lines: Sequence[str], start: int, end: int, max_tokens: int) -> list[Chunk]:
    if _tokens(lines, start, end) <= max_tokens:
        return [_chunk(lines, start, end)]
    out: list[Chunk] = []
    for p_start, p_end in _pack(lines, _paragraphs(lines, start, end), max_tokens):
        if _tokens(lines, p_start, p_end) <= max_tokens:
            out.append(_chunk(lines, p_start, p_end))
            continue
        for l_start, l_end in _pack(lines, [(n, n) for n in range(p_start, p_end + 1)], max_tokens):
            oversized = _tokens(lines, l_start, l_end) > max_tokens  # only ever a single line
            out.append(_chunk(lines, l_start, l_end, oversized))
    return out


def _paragraphs(lines: Sequence[str], start: int, end: int) -> list[tuple[int, int]]:
    """Ranges covering start..end, each ending after a blank line (or at ``end``)."""
    out: list[tuple[int, int]] = []
    p_start = start
    for number in range(start, end + 1):
        if not lines[number - 1].strip() or number == end:
            out.append((p_start, number))
            p_start = number + 1
    return out


def _pack(lines: Sequence[str], pieces: list[tuple[int, int]], max_tokens: int) -> list[tuple[int, int]]:
    """Greedily join consecutive pieces while the joined range stays within the limit."""
    out: list[tuple[int, int]] = []
    for start, end in pieces:
        if out and _tokens(lines, out[-1][0], end) <= max_tokens:
            out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def _chunk(lines: Sequence[str], start: int, end: int, oversized: bool = False) -> Chunk:
    return Chunk(start=start, end=end, lines=tuple(lines[start - 1 : end]), oversized=oversized)
