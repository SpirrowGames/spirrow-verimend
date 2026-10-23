"""run_e2e and ``python -m verimend.eval`` (design 8): extractor + reconciler on the golden set."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from extractor_fakes import FakeClient, chunk_start, http, session

from verimend.eval import __main__ as eval_cli
from verimend.eval import golden
from verimend.eval.e2e import run_e2e
from verimend.eval.golden import GOLDEN_DIR, load_case
from verimend.extractor import Completion, ExtractorAborted, ExtractorConfigError

RECORDED = Path(__file__).parent / "data" / "extractor" / "ctl-01-ports.recorded.json"


class Replay(FakeClient):
    """Answers each chunk with the response recorded from Lexora for it."""

    def __init__(self, recording: dict) -> None:
        super().__init__()
        self.by_start = {r["lines"][0]: r for r in recording["responses"] if r["attempt"] == 0}

    def complete(self, messages, tier):
        self.calls.append(messages)
        r = self.by_start[chunk_start(messages)]
        return Completion(text=r["content"], model=r["model"])


def test_a_golden_case_through_run_e2e_with_recorded_lexora_responses() -> None:
    recording = json.loads(RECORDED.read_text(encoding="utf-8"))
    case = load_case(GOLDEN_DIR / recording["case"])
    s, _ = session(Replay(recording), strict=True)
    result = run_e2e(case, s)
    assert result.extract["failed_chunks"] == []
    assert result.score.layer == "deterministic"
    assert result.score.extraction_recall == 1.0
    assert result.score.total.agree == len(case.claims)
    assert result.score.mismatches == [] and result.score.extra == 0
    assert all(p.result == "verified" for p in result.predictions)


def test_run_e2e_refuses_a_non_strict_session() -> None:
    s, _ = session(FakeClient(), strict=False)
    with pytest.raises(ValueError):
        run_e2e(load_case(GOLDEN_DIR / "ctl-01-ports"), s)


@pytest.fixture
def no_score(monkeypatch):
    called = []
    monkeypatch.setattr(golden, "score", lambda *a, **k: called.append(a))
    return called


def test_a_configuration_error_propagates_and_nothing_is_scored(no_score) -> None:
    s, _ = session(FakeClient(http(401)), strict=True)
    with pytest.raises(ExtractorConfigError):
        run_e2e(load_case(GOLDEN_DIR / "ctl-01-ports"), s)
    assert no_score == []


def test_one_transient_chunk_failure_aborts_and_nothing_is_scored(no_score) -> None:
    s, _ = session(FakeClient(by_chunk=lambda start: http(429) if start > 1 else "[]"), strict=True)
    with pytest.raises(ExtractorAborted):
        run_e2e(load_case(GOLDEN_DIR / "ctl-01-ports"), s)
    assert no_score == []


def test_three_transient_failures_abort_and_nothing_is_scored(no_score) -> None:
    s, _ = session(FakeClient(by_chunk=lambda start: http(503)), strict=True)
    with pytest.raises(ExtractorAborted):
        run_e2e(load_case(GOLDEN_DIR / "ctl-01-ports"), s)
    assert no_score == []


def test_an_unwaitable_retry_after_aborts_and_nothing_is_scored(no_score) -> None:
    s, _ = session(FakeClient(http(429, "9999")), strict=True)
    with pytest.raises(ExtractorAborted):
        run_e2e(load_case(GOLDEN_DIR / "ctl-01-ports"), s)
    assert no_score == []


def test_permanent_failures_are_scored_as_lost_recall() -> None:
    s, _ = session(FakeClient(by_chunk=lambda start: http(422)), strict=True)
    result = run_e2e(load_case(GOLDEN_DIR / "ctl-01-ports"), s)
    assert {f["error"] for f in result.extract["failed_chunks"]} == {"http_422"}
    assert result.score.extraction_recall == 0.0


def test_eval_cli_without_lexora_url_raises_before_scoring(monkeypatch, no_score) -> None:
    monkeypatch.delenv("VERIMEND_LEXORA_URL", raising=False)
    with pytest.raises(ExtractorConfigError) as info:
        eval_cli.main(["--case", "ctl-01-ports"])
    assert info.value.reason == "lexora_url_unset"
    assert no_score == []
