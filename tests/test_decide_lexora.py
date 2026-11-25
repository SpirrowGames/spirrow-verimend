"""T06 Lexora /v1/decide adapter: request shape, the three no-signal causes, response checks."""

import json

import httpx
import pytest

from verimend.decide.lexora import (
    DRIFT_LEVELS,
    DRIFT_QUESTIONS,
    PATCH_RISK_QUESTIONS,
    POLICY_DRIFT,
    QUESTIONS_VERSION_DRIFT,
    LexoraDecideClient,
    drift_judgement,
    parse_response,
    patch_risk_judgement,
)
from verimend.decide.routing import NO_SIGNAL, SIGNAL


def body(answers, provider="jev", decision_id="dec-1", latency_ms=12):
    return {"answers": answers, "provider": provider, "decision_id": decision_id, "latency_ms": latency_ms}


GOOD_DRIFT = {
    "drift": {"score": 2.0, "legend": list(DRIFT_LEVELS), "confidence": 0.8, "probabilities": {"2": 0.8}},
    "mapping_correct": {"noul": 0.9},
}
GOOD_PATCH = {"changes_behavior_description": {"noul": 0.1}, "introduces_claim_not_in_code": {"noul": 0.2}}


def client(handler) -> LexoraDecideClient:
    return LexoraDecideClient("http://lexora:8110/", 5.0, transport=httpx.MockTransport(handler))


def call(c: LexoraDecideClient, questions=DRIFT_QUESTIONS):
    return c.decide(policy=POLICY_DRIFT, state="s", questions=questions, questions_version=QUESTIONS_VERSION_DRIFT)


def test_request_shape_matches_the_contract() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=body(GOOD_DRIFT))

    decision = call(client(handler))
    assert seen["url"] == "http://lexora:8110/v1/decide"
    assert set(seen["body"]) == {"state", "questions", "policy", "questions_version"}
    assert seen["body"]["questions"]["drift"]["type"] == "score"
    assert seen["body"]["questions"]["drift"]["criteria"] == DRIFT_LEVELS  # ordered low to high
    assert seen["body"]["questions"]["mapping_correct"]["type"] == "noul"
    assert decision.signal and decision.decision_id == "dec-1"


def test_stage2_questions_are_noul() -> None:
    # msg-328: 判定 2 stays noul (F2 / choice was not adopted)
    assert {q["type"] for q in PATCH_RISK_QUESTIONS.values()} == {"noul"}
    for q in list(PATCH_RISK_QUESTIONS.values()) + [DRIFT_QUESTIONS["mapping_correct"]]:
        assert set(q["criteria"]) == {"true", "false"}


def test_provider_null_is_no_signal_but_keeps_the_decision_id() -> None:
    decision = call(client(lambda r: httpx.Response(200, json=body({"drift": {"score": 0.0}}, provider="null"))))
    assert not decision.signal
    assert decision.error == "provider_null"
    assert decision.decision_id == "dec-1"  # the shadow-row join key (DoD 8)
    assert drift_judgement(decision).state == NO_SIGNAL


@pytest.mark.parametrize("status", [422, 500, 503, 404])
def test_non_2xx_is_no_signal(status) -> None:
    decision = call(client(lambda r: httpx.Response(status, json={"detail": "x"})))
    assert not decision.signal and decision.error == f"http_{status}" and decision.decision_id is None


def test_transport_failure_is_no_signal() -> None:
    def handler(request):
        raise httpx.ConnectTimeout("slow", request=request)

    decision = call(client(handler))
    assert not decision.signal and decision.error.startswith("transport:")


def test_non_json_body_is_malformed() -> None:
    decision = call(client(lambda r: httpx.Response(200, content=b"<html>")))
    assert not decision.signal and decision.error == "malformed"


def bad(**changes):
    answers = json.loads(json.dumps(GOOD_DRIFT))
    for path, value in changes.items():
        name, key = path.split("__")
        if value is ...:
            answers[name].pop(key)
        else:
            answers[name][key] = value
    return answers


@pytest.mark.parametrize(
    "payload",
    [
        [],
        body(GOOD_DRIFT, decision_id=""),
        body(GOOD_DRIFT, decision_id=None),
        body(GOOD_DRIFT, provider="typesafe"),
        body(GOOD_DRIFT, latency_ms=-1),
        body(GOOD_DRIFT, latency_ms=True),
        body([]),
        body({"drift": GOOD_DRIFT["drift"]}),  # a question missing
        body(bad(mapping_correct__noul=1.5)),
        body(bad(mapping_correct__noul=True)),
        body(bad(mapping_correct__noul="0.9")),
        body(bad(mapping_correct__noul=...)),
        body(bad(drift__confidence=...)),
        body(bad(drift__confidence=1.2)),
        body(bad(drift__score=float("nan"))),
        body(bad(drift__score=3.5)),  # off the four-level legend
        body(bad(drift__score=-0.1)),
        body(bad(drift__legend={"0": "a"})),
        body(bad(drift__legend=["a", "b"])),  # not the legend that was sent
        body(bad(drift__legend=["a", "b", "c", "d"])),  # same length, other labels
        body(bad(drift__legend=list(reversed(DRIFT_LEVELS)), drift__score=0.0, drift__confidence=0.95)),  # reversed
        body(bad(drift__legend=[*DRIFT_LEVELS[:2], DRIFT_LEVELS[3], DRIFT_LEVELS[2]])),  # two levels swapped
        body(bad(drift__legend=[1, 2, 3, 4])),
        body({"drift": "2", "mapping_correct": {"noul": 0.5}}),
    ],
)
def test_malformed_responses_are_no_signal(payload) -> None:
    decision = parse_response(payload, DRIFT_QUESTIONS)
    assert not decision.signal
    assert decision.error == "malformed"
    assert drift_judgement(decision).state == NO_SIGNAL


def test_drift_judgement_from_a_signal() -> None:
    j = drift_judgement(parse_response(body(GOOD_DRIFT), DRIFT_QUESTIONS))
    assert j.state == SIGNAL
    assert (j.drift.value, j.drift.confidence, j.mapping_correct, j.decision_id) == (2.0, 0.8, 0.9, "dec-1")


def test_patch_risk_judgement_from_a_signal_and_without_one() -> None:
    j = patch_risk_judgement(parse_response(body(GOOD_PATCH, decision_id="p"), PATCH_RISK_QUESTIONS))
    assert (j.signal, j.changes_behavior_description, j.introduces_claim_not_in_code, j.decision_id) == (
        True, 0.1, 0.2, "p",
    )
    none = patch_risk_judgement(parse_response(body(GOOD_PATCH, provider="null"), PATCH_RISK_QUESTIONS))
    assert not none.signal and none.changes_behavior_description is None


def test_unrequested_answers_are_ignored() -> None:
    decision = parse_response(body({**GOOD_PATCH, "extra": {"noul": 9}}), PATCH_RISK_QUESTIONS)
    assert decision.signal and set(decision.answers) == set(PATCH_RISK_QUESTIONS)
