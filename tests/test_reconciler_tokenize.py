"""The BM25 tokenizer (verimend/reconciler/tokenize.py, design 5.3)."""

from __future__ import annotations

from verimend.reconciler.tokenize import tokenize


def test_alnum_runs_are_one_lowercased_token() -> None:
    assert tokenize("Set VERIMEND_LEXORA_URL to http://host:8110/v1") == [
        "set", "verimend_lexora_url", "to", "http", "host", "8110/v1",  # edge "/" stripped
    ]


def test_sentence_punctuation_does_not_stick_to_a_token() -> None:
    assert tokenize("The REST API listens on port 8113.") == ["the", "rest", "api", "listens", "on", "port", "8113"]
    assert tokenize("see config.yaml, then src/app.py") == ["see", "config.yaml", "then", "src/app.py"]


def test_cjk_runs_become_character_bigrams() -> None:
    assert tokenize("設定ファイル") == ["設定", "定フ", "ファ", "ァイ", "イル"]


def test_mixed_english_and_japanese_gives_both_kinds_of_token() -> None:
    assert tokenize("`VERIMEND_LEXORA_URL` を設定ファイルに書く") == [
        "verimend_lexora_url", "を設", "設定", "定フ", "ファ", "ァイ", "イル", "ルに", "に書", "書く",
    ]


def test_full_width_alnum_is_the_same_token_as_half_width() -> None:
    assert tokenize("ＰＯＲＴ　８１１３") == tokenize("port 8113") == ["port", "8113"]


def test_half_width_katakana_is_normalised_to_full_width() -> None:
    assert tokenize("ﾎﾟｰﾄ") == tokenize("ポート")


def test_a_single_cjk_character_is_its_own_token() -> None:
    assert tokenize("港 8113 番") == ["港", "8113", "番"]


def test_prolonged_sound_mark_stays_inside_the_run() -> None:
    assert tokenize("サーバー") == ["サー", "ーバ", "バー"]


def test_full_width_punctuation_separates() -> None:
    assert tokenize("設定。ポート、番号") == ["設定", "ポー", "ート", "番号"]


def test_nothing_to_tokenize() -> None:
    assert tokenize("") == [] and tokenize("!!! --- ...") == []
