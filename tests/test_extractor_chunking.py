"""Deterministic chunking (design 5.2): headings, then paragraphs, then lines."""

from verimend.extractor.chunking import MAX_CHUNK_TOKENS, chunk_document, chunk_hash, estimate_tokens


def test_splits_at_headings_and_numbers_lines_absolutely() -> None:
    text = "intro\n\n# A\na1\na2\n## B\nb1\n"
    chunks = chunk_document(text)
    assert [c.span for c in chunks] == [(1, 2), (3, 5), (6, 7)]
    assert chunks[1].lines == ("# A", "a1", "a2")
    assert not any(c.oversized for c in chunks)


def test_hash_inside_a_fenced_code_block_is_not_a_heading() -> None:
    text = "# Setup\n```bash\n# install\npip install x\n```\n~~~\n# also code\n~~~\n# Next\nn\n"
    assert [c.span for c in chunk_document(text)] == [(1, 8), (9, 10)]


def test_blank_only_chunks_are_dropped() -> None:
    assert [c.span for c in chunk_document("\n\n# A\nx\n")] == [(3, 4)]
    assert chunk_document("") == []


def test_long_section_splits_at_paragraph_boundaries() -> None:
    para = "word " * 600  # ~750 tokens per paragraph line
    text = "# Big\n" + "\n\n".join([para, para, para, para]) + "\n"
    chunks = chunk_document(text)
    assert len(chunks) > 1
    for c in chunks:
        assert estimate_tokens("\n".join(c.lines)) <= MAX_CHUNK_TOKENS
        assert not c.oversized
    # every boundary falls after a blank line (a paragraph end) or at the end
    lines = text.splitlines()
    for c in chunks[:-1]:
        assert lines[c.end - 1].strip() == ""
    assert chunks[0].start == 1 and chunks[-1].end == len(lines)


def test_long_paragraph_splits_at_line_boundaries() -> None:
    line = "x" * 3000  # 750 tokens
    text = "# P\n" + "\n".join([line] * 5) + "\n"  # one paragraph, ~3750 tokens
    chunks = chunk_document(text)
    assert len(chunks) >= 2
    assert all(not c.oversized for c in chunks)
    covered = [n for c in chunks for n in range(c.start, c.end + 1)]
    assert covered == list(range(1, 7))


def test_a_single_line_over_the_limit_is_its_own_oversized_chunk() -> None:
    text = "# T\nshort\n" + "y" * (MAX_CHUNK_TOKENS * 4 + 8) + "\nafter\n"
    chunks = chunk_document(text)
    oversized = [c for c in chunks if c.oversized]
    assert [c.span for c in oversized] == [(3, 3)]
    assert all(c.span != (3, 3) for c in chunks if not c.oversized)


def test_non_ascii_counts_one_token_per_character() -> None:
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("ポート") == 3


def test_chunk_hash_depends_on_the_boundaries_only() -> None:
    a = chunk_document("# A\nx\n# B\ny\n")
    b = chunk_document("# A\nchanged\n# B\nalso changed\n")
    c = chunk_document("# A\nx\ny\n# B\n")
    assert chunk_hash(a) == chunk_hash(b)
    assert chunk_hash(a) != chunk_hash(c)
