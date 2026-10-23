"""The deterministic tokenizer of the BM25 retrieval (docs/design.md 5.3). No morphological analyser.

``tokenize(text)`` is a pure function, applied identically to the query (a
claim) and to the documents (facts):

1. NFKC normalisation, so full-width ``ＡＢＣ１２３`` and half-width katakana
   become ``ABC123`` and full-width katakana.
2. A run of ASCII letters, digits and ``_ - . /`` is one token, lowercased
   (``VERIMEND_LEXORA_URL`` -> ``verimend_lexora_url``). Leading and trailing
   ``. - /`` are stripped from it, so the period ending a sentence does not
   turn ``8113.`` into a token that never matches ``8113``.
3. A run of CJK characters -- hiragana, katakana (with the prolonged sound
   mark ``ー``, and the half-width block for safety), CJK Unified Ideographs
   and Extension A -- is split into character bigrams: ``設定ファイル`` ->
   ``設定 定フ ファ ァイ イル``. A run of one character is that character.
4. Everything else (whitespace, symbols, full-width punctuation) separates.

Bigrams are what keep Japanese text searchable: BM25 matches exact tokens, and
without them a Japanese claim's query would be empty, so it would find no
candidate facts and be settled ``unverifiable`` without the LLM ever seeing it.
"""

from __future__ import annotations

import re
import unicodedata

_ALNUM = r"[A-Za-z0-9_\-./]+"
_CJK = r"[぀-ゟ゠-ヿｦ-ﾟ㐀-䶿一-鿿]+"
_TOKEN_RUN = re.compile(f"(?P<alnum>{_ALNUM})|(?P<cjk>{_CJK})")
_EDGE = ".-/"


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_RUN.finditer(unicodedata.normalize("NFKC", text)):
        if match.group("alnum") is not None:
            token = match.group("alnum").strip(_EDGE).lower()
            if token:
                tokens.append(token)
        else:
            run = match.group("cjk")
            if len(run) == 1:
                tokens.append(run)
            else:
                tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens
