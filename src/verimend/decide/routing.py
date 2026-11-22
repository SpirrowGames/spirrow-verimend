"""The T06 decision rules, as pure functions (thread T-decide-drift-score, msg-047 / msg-317 / msg-319 / msg-321, as ruled by msg-328 / msg-329).

Nothing here talks to Lexora or the database. The inputs are what a judgement
returned, already classified into a state; the outputs are ``outcome`` values
of the ``decision`` table (``OUTCOMES``) and PR labels.

**判定 1 (drift, per verdict, before the mender).** State is one of three:

- ``exempt``: ``verdict.method == "deterministic"``. Decided by Verimend before
  any call, from the verdict alone -- never from a response -- so an error
  response cannot be mistaken for an exemption. Lexora is not called.
- ``no_signal``: ``provider == "null"``, a send / receive failure (timeout,
  5xx, unreachable), or a malformed response. Nothing else.
- ``signal``: a real answer came back.

``drift_gate`` turns it into the stage-1 outcome: ``no_signal`` and a low
confidence never skip generation; a confident low drift or a confident wrong
mapping skips it.

**判定 2 (patch_risk, per mender group, after the draft).** ``route_patch``
evaluates 分岐 0 then 1..8, in order (分岐 3 is a vacant number, see below). 分岐 0 settles a group whose stage-1
classes are not all the same as ``review_mixed_group`` before anything else,
so the "findings_only from stage 2 alone" rule (分岐 4) only ever sees a
non-mixed group. ``auto`` needs every condition met explicitly (分岐 7);
anything left over is ``review`` (分岐 8, default-deny).

判定 2's two questions are ``noul``: Lexora's contract gives a noul answer a
probability and no confidence. By the human ruling of msg-328 (F1(b)), 判定 2
carries no confidence condition: the old 分岐 3 ("a stage-2 answer below
``min_confidence``") is removed and its number left vacant so the thread's
branch numbers still apply, and 分岐 7 no longer reads a stage-2 confidence.
Uncertainty in 判定 2 shows in the probability itself: anything in
``(auto_pr_max_risk, findings_only_min_claim_risk)`` lands on 分岐 5 (review).
判定 1's ``drift`` is a ``score`` and keeps its confidence (low_conf, 分岐 6 / 7).

**PR label.** ``pr_label`` ranks review > no label > auto over the drafts that
go into one PR; ``findings_only`` drafts are not in the PR and do not count.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

STAGE_DRIFT = "drift"
STAGE_PATCH_RISK = "patch_risk"

MODE_OFF = "off"
MODE_OBSERVE = "observe"
MODE_ENFORCE = "enforce"

# stage-1 states
SIGNAL = "signal"
NO_SIGNAL = "no_signal"
EXEMPT = "exempt"

# outcomes (the decision table's CHECK list)
OUT_NO_SIGNAL = "no_signal"
OUT_EXEMPT = "exempt"
OUT_SKIP_DRIFT = "skip_drift"
OUT_SKIP_MAPPING = "skip_mapping"
OUT_GENERATED_LOW_CONF = "generated_low_conf"
OUT_GENERATED = "generated"
OUT_AUTO = "auto"
OUT_REVIEW = "review"
OUT_REVIEW_NO_PATCH_SIGNAL = "review_no_patch_signal"
OUT_REVIEW_MIXED_GROUP = "review_mixed_group"
OUT_FINDINGS_ONLY = "findings_only"
OUTCOMES = (
    OUT_NO_SIGNAL, OUT_EXEMPT, OUT_SKIP_DRIFT, OUT_SKIP_MAPPING, OUT_GENERATED_LOW_CONF, OUT_GENERATED,
    OUT_AUTO, OUT_REVIEW, OUT_REVIEW_NO_PATCH_SIGNAL, OUT_REVIEW_MIXED_GROUP, OUT_FINDINGS_ONLY,
)
STAGE1_SKIPS = frozenset({OUT_SKIP_DRIFT, OUT_SKIP_MAPPING})

# PR labels; None is "no label"
LABEL_AUTO = "verimend:auto"
LABEL_REVIEW = "verimend:review"

# stage-1 classes a mender group is compared by (msg-321: low_conf and not are different classes)
CLASS_NO_SIGNAL = "no_signal"
CLASS_SIGNAL_LOW_CONF = "signal_low_conf"
CLASS_SIGNAL = "signal"
CLASS_EXEMPT = "exempt"
MIXED = "mixed"


@dataclass(frozen=True)
class Thresholds:
    drift_min_level: int = 1
    min_mapping_correct: float = 0.40
    auto_pr_max_risk: float = 0.25
    min_confidence: float = 0.60
    findings_only_min_claim_risk: float = 0.75

    @classmethod
    def from_settings(cls, settings) -> "Thresholds":
        return cls(
            drift_min_level=settings.decide_drift_min_level,
            min_mapping_correct=settings.decide_min_mapping_correct,
            auto_pr_max_risk=settings.decide_auto_pr_max_risk,
            min_confidence=settings.decide_min_confidence,
            findings_only_min_claim_risk=settings.decide_findings_only_min_claim_risk,
        )


@dataclass(frozen=True)
class Answer:
    """One answered question: its value (a 0..3 level or a 0..1 probability) and the confidence in it."""

    value: float
    confidence: float


@dataclass(frozen=True)
class DriftJudgement:
    """判定 1 for one verdict. ``drift`` / ``mapping_correct`` are set exactly when ``state == SIGNAL``.

    ``drift`` is a ``score`` (a 0..3 level and its confidence); ``mapping_correct``
    is a ``noul`` probability, which has no confidence in Lexora's contract.
    """

    state: str  # SIGNAL | NO_SIGNAL | EXEMPT
    drift: Answer | None = None
    mapping_correct: float | None = None
    decision_id: str | None = None


@dataclass(frozen=True)
class PatchRiskJudgement:
    """判定 2 for one group's draft: two noul probabilities, set exactly when ``signal`` (no confidence)."""

    signal: bool
    changes_behavior_description: float | None = None
    introduces_claim_not_in_code: float | None = None
    decision_id: str | None = None


# ----------------------------------------------------------------- 判定 1


def drift_low_confidence(judgement: DriftJudgement, t: Thresholds) -> bool:
    """A ``signal`` judgement whose ``drift`` confidence is below ``min_confidence`` (the only confidence 判定 1 has)."""
    if judgement.state != SIGNAL:
        return False
    assert judgement.drift is not None
    return judgement.drift.confidence < t.min_confidence


def drift_gate(judgement: DriftJudgement, t: Thresholds) -> str:
    """The stage-1 outcome. Only ``skip_drift`` / ``skip_mapping`` keep the verdict from the mender."""
    if judgement.state == EXEMPT:
        return OUT_EXEMPT
    if judgement.state == NO_SIGNAL:
        return OUT_NO_SIGNAL  # generate: a missing signal never skips
    if drift_low_confidence(judgement, t):
        return OUT_GENERATED_LOW_CONF
    assert judgement.drift is not None and judgement.mapping_correct is not None
    if judgement.drift.value < t.drift_min_level:
        return OUT_SKIP_DRIFT
    if judgement.mapping_correct < t.min_mapping_correct:
        return OUT_SKIP_MAPPING
    return OUT_GENERATED


def stage1_class(judgement: DriftJudgement, t: Thresholds) -> str:
    if judgement.state == EXEMPT:
        return CLASS_EXEMPT
    if judgement.state == NO_SIGNAL:
        return CLASS_NO_SIGNAL
    return CLASS_SIGNAL_LOW_CONF if drift_low_confidence(judgement, t) else CLASS_SIGNAL


@dataclass(frozen=True)
class GroupStage1:
    """The stage-1 side of one mender group, as 判定 2 reads it."""

    cls: str  # one CLASS_* or MIXED
    drift_confidence: float | None = None  # the lowest drift confidence of a CLASS_SIGNAL group


def group_stage1(judgements: Sequence[DriftJudgement], t: Thresholds) -> GroupStage1:
    """The group's class: the members' common class, or ``MIXED`` when they differ (分岐 0)."""
    classes = {stage1_class(j, t) for j in judgements}
    if len(classes) != 1:
        return GroupStage1(MIXED)
    (cls,) = classes
    if cls == CLASS_SIGNAL:
        return GroupStage1(cls, min(j.drift.confidence for j in judgements if j.drift is not None))
    return GroupStage1(cls)


# ----------------------------------------------------------------- 判定 2


def route_patch(stage1: GroupStage1, patch: PatchRiskJudgement, t: Thresholds) -> str:
    """分岐 0, then 1..8, in order (msg-047 as amended by msg-045 / msg-321 / msg-328; 分岐 3 vacant)."""
    # 0. the stage-1 classes differ within the group: settled before any other branch
    if stage1.cls == MIXED:
        return OUT_REVIEW_MIXED_GROUP
    stage1_signal = stage1.cls in (CLASS_SIGNAL, CLASS_SIGNAL_LOW_CONF)
    if not patch.signal:
        # 1. no stage-2 signal, stage 1 no_signal / exempt: today's behaviour
        if not stage1_signal:
            return OUT_NO_SIGNAL
        # 2. Lexora answers stage 1 but not stage 2: abnormal
        return OUT_REVIEW_NO_PATCH_SIGNAL
    behaviour = patch.changes_behavior_description
    claim = patch.introduces_claim_not_in_code
    assert behaviour is not None and claim is not None
    # 3. vacant (removed by msg-328 F1(b): a noul answer has no confidence)
    # 4. a likely hallucination: no PR, findings only (stage 2 alone decides, non-mixed groups only)
    if claim >= t.findings_only_min_claim_risk:
        return OUT_FINDINGS_ONLY
    # 5. either risk over the auto ceiling
    if behaviour > t.auto_pr_max_risk or claim > t.auto_pr_max_risk:
        return OUT_REVIEW
    # 6. stage 1 missing or not confident (never hit by exempt)
    if stage1.cls in (CLASS_NO_SIGNAL, CLASS_SIGNAL_LOW_CONF):
        return OUT_REVIEW
    # 7. auto only when every condition holds explicitly (stage-1 drift confidence only; none from stage 2)
    stage1_ok = stage1.cls == CLASS_EXEMPT or (
        stage1.cls == CLASS_SIGNAL
        and stage1.drift_confidence is not None
        and stage1.drift_confidence >= t.min_confidence
    )
    if (
        stage1_ok
        and behaviour <= t.auto_pr_max_risk
        and claim <= t.auto_pr_max_risk
        and claim < t.findings_only_min_claim_risk
    ):
        return OUT_AUTO
    # 8. default-deny
    return OUT_REVIEW


# ---------------------------------------------------------------- PR label


def outcome_label(outcome: str | None) -> str | None:
    """The label one draft asks for. ``None`` (no applicable enforce row) and ``no_signal`` ask for none."""
    if outcome == OUT_AUTO:
        return LABEL_AUTO
    if outcome in (OUT_REVIEW, OUT_REVIEW_NO_PATCH_SIGNAL, OUT_REVIEW_MIXED_GROUP):
        return LABEL_REVIEW
    return None


def pr_label(outcomes: Iterable[str | None]) -> str | None:
    """review > no label > auto over the drafts in one PR (``findings_only`` ones are not in it)."""
    labels = [outcome_label(o) for o in outcomes if o != OUT_FINDINGS_ONLY]
    if not labels:
        return None
    if LABEL_REVIEW in labels:
        return LABEL_REVIEW
    if None in labels:
        return None
    return LABEL_AUTO
