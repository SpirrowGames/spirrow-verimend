"""The Lexora ``POST /v1/decide`` adapter for 判定 1 / 判定 2 (T06, thread T-decide-drift-score).

Contract source: Lexora ``src/lexora/decide/contract.py`` / ``jev_client.py`` /
``routes.py`` at ``3e19e9052c0a29ecc94115afdcc3264fa8555586`` (origin/main when
T06 was implemented; quoted in msg-324, re-checked with ``git show`` for DoD 8').

Request ``{"state", "questions": {name: {"type", "instructions", "criteria"}},
"policy", "questions_version"}``. Response ``{"answers", "provider",
"decision_id", "latency_ms"}``; a ``noul`` answer is ``{"noul": p}`` and has no
confidence, a ``score`` answer is ``{"score", "legend": [str], "confidence"}``.

``decide`` never raises for anything Lexora does. It returns a ``Decision``
whose ``signal`` is False on exactly three things (msg-324 / msg-329):

1. ``provider == "null"`` -- including Lexora's own fallback from a failed Jev
   call, which arrives as 200 + ``provider:"null"``;
2. a send / receive failure: timeout, refused connection, any non-2xx (5xx,
   422 for a malformed request, ...);
3. a malformed response: anything that fails ``_check``.

``decision_id`` is kept whenever a well-formed response came back, also on a
``null`` one: under Lexora's shadow mode the answering row is ``null`` and the
answers worth evaluating sit on the shadow row, joined by
``decisions.shadow_of = decision.decision_id`` (DoD 8).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from verimend.decide.routing import Answer, DriftJudgement, NO_SIGNAL, PatchRiskJudgement, SIGNAL

POLICY_DRIFT = "verimend.drift"
POLICY_PATCH_RISK = "verimend.patch_risk"
QUESTIONS_VERSION_DRIFT = "drift-v1"
QUESTIONS_VERSION_PATCH_RISK = "patch-risk-v1"

# 判定 1: drift is a score on four levels, low to high (msg-023)
DRIFT_LEVELS = ["一致", "軽微な表現差", "事実が異なる", "機能自体が存在しない"]

DRIFT_QUESTIONS: dict[str, dict[str, Any]] = {
    "drift": {
        "type": "score",
        "instructions": (
            "How far does the documentation passage in the state diverge from the code fragment it is "
            "mapped to? Judge the facts the passage states, not its wording."
        ),
        "criteria": DRIFT_LEVELS,
    },
    "mapping_correct": {
        "type": "noul",
        "instructions": "Does the documentation passage really describe the code fragment it is mapped to?",
        "criteria": {
            "true": "The passage is about this code: the same component, command, setting or behaviour.",
            "false": "The passage is about something else, and the mapping to this code is a mistake.",
        },
    },
}

PATCH_RISK_QUESTIONS: dict[str, dict[str, Any]] = {
    "changes_behavior_description": {
        "type": "noul",
        "instructions": "Does the diff in the state change what the documentation says the code does?",
        "criteria": {
            "true": "A statement about the code's behaviour, interface or values is changed.",
            "false": "Only wording, formatting or presentation changes; every behaviour statement is kept.",
        },
    },
    "introduces_claim_not_in_code": {
        "type": "noul",
        "instructions": (
            "Does the diff add a factual statement that the code fragment in the state does not support?"
        ),
        "criteria": {
            "true": "The new text states a fact that is not in the code fragment.",
            "false": "Every fact in the new text is found in the code fragment.",
        },
    },
}


@dataclass(frozen=True)
class Decision:
    """One ``/v1/decide`` call as Verimend sees it. ``answers`` is set exactly when ``signal``."""

    signal: bool
    decision_id: str | None = None
    provider: str | None = None
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None  # why there is no signal: provider_null | transport | http_<n> | malformed


class DecideClient(Protocol):
    def decide(
        self, *, policy: str, state: str, questions: dict[str, dict[str, Any]], questions_version: str
    ) -> Decision: ...


class LexoraDecideClient:
    """``DecideClient`` over httpx. ``timeout_s`` is the httpx timeout and nothing else; no retry."""

    def __init__(self, url: str, timeout_s: float, transport: httpx.BaseTransport | None = None) -> None:
        self.endpoint = url.rstrip("/") + "/v1/decide"
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    def decide(
        self, *, policy: str, state: str, questions: dict[str, dict[str, Any]], questions_version: str
    ) -> Decision:
        body = {"state": state, "questions": questions, "policy": policy, "questions_version": questions_version}
        try:
            response = self._http.post(self.endpoint, json=body)
        except httpx.TransportError as exc:  # timeouts, connect / read errors
            return Decision(False, error=f"transport:{exc.__class__.__name__}")
        if not response.is_success:
            return Decision(False, error=f"http_{response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            return Decision(False, error="malformed")
        return parse_response(payload, questions)

    def close(self) -> None:
        self._http.close()


def parse_response(payload: object, questions: dict[str, dict[str, Any]]) -> Decision:
    """Check a 2xx body against the contract; anything off is ``malformed`` (no signal)."""
    if not isinstance(payload, dict):
        return Decision(False, error="malformed")
    decision_id = payload.get("decision_id")
    provider = payload.get("provider")
    latency = payload.get("latency_ms")
    answers = payload.get("answers")
    if (
        not isinstance(decision_id, str)
        or not decision_id
        or provider not in ("null", "llm", "jev")
        or not _is_int(latency)
        or latency < 0
        or not isinstance(answers, dict)
    ):
        return Decision(False, error="malformed")
    if provider == "null":
        return Decision(False, decision_id=decision_id, provider=provider, error="provider_null")
    out: dict[str, dict[str, Any]] = {}
    for name, spec in questions.items():
        raw = answers.get(name)
        if not _answer_ok(spec, raw):
            return Decision(False, decision_id=decision_id, provider=provider, error="malformed")
        out[name] = raw
    return Decision(True, decision_id=decision_id, provider=provider, answers=out)


def _answer_ok(spec: dict[str, Any], raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    if spec["type"] == "noul":
        return _is_prob(raw.get("noul"))
    if spec["type"] == "score":
        legend = raw.get("legend")
        score = raw.get("score")
        if not (
            _is_number(score)
            and _is_prob(raw.get("confidence"))
            and isinstance(legend, list)
            and legend
            and all(isinstance(level, str) for level in legend)
        ):
            return False
        # Verimend reads the score as a level on the legend it sent: a different
        # legend or a position off it cannot be read as one.
        criteria = spec.get("criteria")
        if isinstance(criteria, list) and len(legend) != len(criteria):
            return False
        return 0.0 <= score <= len(legend) - 1
    return False  # Verimend asks only noul / score questions


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_prob(value: object) -> bool:
    return _is_number(value) and 0.0 <= value <= 1.0


# --------------------------------------------------------- to the routing types


def drift_judgement(decision: Decision) -> DriftJudgement:
    """判定 1's ``Decision`` as a routing state (``exempt`` is never made here: it makes no call)."""
    if not decision.signal:
        return DriftJudgement(NO_SIGNAL, decision_id=decision.decision_id)
    drift = decision.answers["drift"]
    return DriftJudgement(
        SIGNAL,
        Answer(float(drift["score"]), float(drift["confidence"])),
        float(decision.answers["mapping_correct"]["noul"]),
        decision.decision_id,
    )


def patch_risk_judgement(decision: Decision) -> PatchRiskJudgement:
    """判定 2's ``Decision`` as a routing state: two noul probabilities, no confidence (msg-328)."""
    if not decision.signal:
        return PatchRiskJudgement(False, decision_id=decision.decision_id)
    return PatchRiskJudgement(
        True,
        float(decision.answers["changes_behavior_description"]["noul"]),
        float(decision.answers["introduces_claim_not_in_code"]["noul"]),
        decision.decision_id,
    )
