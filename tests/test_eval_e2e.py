"""run_e2e and ``python -m verimend.eval`` (design 8): extractor + reconciler on the golden set."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from extractor_fakes import FakeClient, chunk_start, http, session

from verimend.eval import __main__ as eval_cli
from verimend.eval import golden
from verimend.eval.e2e import run_e2e
from verimend.eval.golden import GOLDEN_DIR, load_case
from verimend.extractor import Completion, ExtractorAborted, ExtractorConfigError
from verimend.reconciler import JudgeAborted, JudgeConfigError, JudgeSession

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


# ------------------------------------------------------- the final layer (M2c)

RECORDED_FINAL = Path(__file__).parent / "data" / "reconciler" / "trap-04-negated-historical.recorded.json"


class JudgeReplay(FakeClient):
    """Answers each claim with the judgement recorded from Lexora for it (keyed by the claim text)."""

    def __init__(self, recording: dict) -> None:
        super().__init__()
        self.by_claim = {r["claim"]: r for r in recording["judge_responses"] if r["attempt"] == 0}

    def complete(self, messages, tier):
        self.calls.append(messages)
        r = self.by_claim[messages[1]["content"].splitlines()[1]]
        return Completion(text=r["content"], model=r["model"])


def _judge(client, **kwargs):
    return JudgeSession(client, prompt="judge prompt", sleep=lambda s: None, strict=True, **kwargs)


def test_the_final_layer_through_run_e2e_with_recorded_lexora_responses() -> None:
    recording = json.loads(RECORDED_FINAL.read_text(encoding="utf-8"))
    case = load_case(GOLDEN_DIR / recording["case"])
    s, _ = session(Replay(recording), strict=True)
    judge_client = JudgeReplay(recording)
    result = run_e2e(case, s, _judge(judge_client))
    assert result.score.layer == "deterministic" and result.score.total.agree == len(case.claims)
    assert result.final_score.layer == "final"
    assert result.final_score.extraction_recall == 1.0
    # As recorded: c3 (negated 8000) and c5 (removed command, no candidate facts) end unverifiable.
    assert result.final_score.total.agree == 3
    assert sorted(m.claim_id for m in result.final_score.mismatches) == ["c3", "c5"]
    assert len(judge_client.calls) == 3  # c5 had no candidate facts: no call
    assert result.reconcile["llm_verified"] == 2 and result.reconcile["llm_unverifiable"] == 2
    assert result.reconcile["deferred"] == 0


def _trap04_extraction():
    """trap-04 (four claims the checkers defer) and a strict session replaying its recorded extraction."""
    recording = json.loads(RECORDED_FINAL.read_text(encoding="utf-8"))
    return load_case(GOLDEN_DIR / recording["case"]), session(Replay(recording), strict=True)[0]


@pytest.mark.parametrize("status", [401, 404])
def test_a_judge_configuration_error_propagates_and_nothing_is_scored(no_score, status) -> None:
    case, s = _trap04_extraction()
    with pytest.raises(JudgeConfigError):
        run_e2e(case, s, _judge(FakeClient(http(status))))
    assert no_score == []


def test_a_judge_transient_failure_aborts_and_nothing_is_scored(no_score) -> None:
    case, s = _trap04_extraction()
    with pytest.raises(JudgeAborted):
        run_e2e(case, s, _judge(FakeClient(http(503), http(503))))
    assert no_score == []


def test_three_400s_from_the_judge_abort_and_nothing_is_scored(no_score) -> None:
    case, s = _trap04_extraction()
    with pytest.raises(JudgeAborted) as info:
        run_e2e(case, s, _judge(FakeClient(http(400), http(400), http(400))))
    assert info.value.reason == "permanent_http_streak"
    assert no_score == []


def test_an_extraction_404_is_a_configuration_error_and_nothing_is_scored(no_score) -> None:
    s, _ = session(FakeClient(http(404)), strict=True)
    with pytest.raises(ExtractorConfigError) as info:
        run_e2e(load_case(GOLDEN_DIR / "ctl-01-ports"), s)
    assert info.value.reason == "http_404"
    assert no_score == []


def test_three_400s_in_extraction_abort_and_nothing_is_scored(no_score, tmp_path) -> None:
    # The golden cases are one or two chunks; give a copy of ctl-01 two more sections.
    case_dir = tmp_path / "ctl-01-ports"
    shutil.copytree(GOLDEN_DIR / "ctl-01-ports", case_dir)
    with (case_dir / "doc.md").open("a", encoding="utf-8") as doc:
        doc.write("\n# Extra one\n\nx\n\n# Extra two\n\ny\n")
    s, _ = session(FakeClient(by_chunk=lambda start: http(400)), strict=True)
    with pytest.raises(ExtractorAborted) as info:
        run_e2e(load_case(case_dir), s)
    assert info.value.reason == "permanent_http_streak"
    assert no_score == []


def test_run_e2e_refuses_a_non_strict_judge() -> None:
    case, s = _trap04_extraction()
    with pytest.raises(ValueError):
        run_e2e(case, s, JudgeSession(FakeClient(), prompt="p", strict=False))


def test_eval_cli_on_an_empty_golden_set_says_so_and_exits_non_zero(tmp_path, capsys, no_score) -> None:
    assert eval_cli.main(["--root", str(tmp_path)]) != 0
    assert f"no golden cases under {tmp_path}" in capsys.readouterr().err
    assert no_score == []
