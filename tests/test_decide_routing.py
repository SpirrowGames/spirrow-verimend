"""T06 decision rules (thread T-decide-drift-score): 判定 1 gate, 分岐 0..8, the PR label."""

import pytest

from verimend.decide.routing import (
    CLASS_EXEMPT,
    CLASS_NO_SIGNAL,
    CLASS_SIGNAL,
    CLASS_SIGNAL_LOW_CONF,
    EXEMPT,
    LABEL_AUTO,
    LABEL_REVIEW,
    MIXED,
    NO_SIGNAL,
    OUT_AUTO,
    OUT_EXEMPT,
    OUT_FINDINGS_ONLY,
    OUT_GENERATED,
    OUT_GENERATED_LOW_CONF,
    OUT_NO_SIGNAL,
    OUT_REVIEW,
    OUT_REVIEW_MIXED_GROUP,
    OUT_REVIEW_NO_PATCH_SIGNAL,
    OUT_SKIP_DRIFT,
    OUT_SKIP_MAPPING,
    SIGNAL,
    Answer,
    DriftJudgement,
    GroupStage1,
    PatchRiskJudgement,
    Thresholds,
    drift_gate,
    group_stage1,
    pr_label,
    route_patch,
)

T = Thresholds()


def drift(level: float, conf: float = 0.9, mapping: float = 0.9) -> DriftJudgement:
    return DriftJudgement(SIGNAL, Answer(level, conf), mapping, "d-1")


def patch(behaviour: float, claim: float) -> PatchRiskJudgement:
    return PatchRiskJudgement(True, behaviour, claim, "p-1")


NO_PATCH = PatchRiskJudgement(False)
SAFE = patch(0.1, 0.1)
EX = DriftJudgement(EXEMPT)
NS = DriftJudgement(NO_SIGNAL)
SIG = drift(2)
LOW = drift(2, conf=0.3)


# ------------------------------------------------------------------ 判定 1


@pytest.mark.parametrize(
    ("judgement", "expected"),
    [
        (EX, OUT_EXEMPT),
        (NS, OUT_NO_SIGNAL),  # generate, never skip
        (drift(0, conf=0.3), OUT_GENERATED_LOW_CONF),  # a low drift with low confidence is not skipped
        (drift(2, conf=0.3, mapping=0.0), OUT_GENERATED_LOW_CONF),  # low drift confidence: a wrong mapping is not skipped either
        (drift(2, conf=0.59), OUT_GENERATED_LOW_CONF),
        (drift(0, conf=0.60), OUT_SKIP_DRIFT),
        (drift(0), OUT_SKIP_DRIFT),
        (drift(2, mapping=0.39), OUT_SKIP_MAPPING),
        (drift(1, mapping=0.40), OUT_GENERATED),
        (drift(3), OUT_GENERATED),
    ],
)
def test_drift_gate(judgement, expected) -> None:
    assert drift_gate(judgement, T) == expected


@pytest.mark.parametrize(
    ("members", "cls"),
    [
        ([EX, EX], CLASS_EXEMPT),
        ([NS], CLASS_NO_SIGNAL),
        ([SIG, drift(1, conf=0.7)], CLASS_SIGNAL),
        ([LOW, LOW], CLASS_SIGNAL_LOW_CONF),
        ([EX, SIG], MIXED),  # exempt only when every member is deterministic
        ([NS, LOW], MIXED),
        ([SIG, LOW], MIXED),  # low_conf and not are different classes (msg-321)
    ],
)
def test_group_stage1_class(members, cls) -> None:
    assert group_stage1(members, T).cls == cls


def test_group_stage1_keeps_the_lowest_drift_confidence() -> None:
    assert group_stage1([drift(2, conf=0.95), drift(2, conf=0.7)], T).drift_confidence == 0.7


# ------------------------------------------------------------------ 判定 2


def g(*members: DriftJudgement) -> GroupStage1:
    return group_stage1(list(members), T)


@pytest.mark.parametrize(
    ("name", "stage1", "stage2", "expected"),
    [
        # 分岐 0: mixed groups are review before anything else (msg-321)
        ("0 mixed x high claim risk", g(NS, LOW), patch(0.1, 0.9), OUT_REVIEW_MIXED_GROUP),
        ("0 mixed x no stage-2 signal", g(NS, LOW), NO_PATCH, OUT_REVIEW_MIXED_GROUP),
        ("0 mixed x safe", g(EX, SIG), SAFE, OUT_REVIEW_MIXED_GROUP),
        # 分岐 1
        ("1 no/no", g(NS), NO_PATCH, OUT_NO_SIGNAL),
        ("1 exempt/no", g(EX), NO_PATCH, OUT_NO_SIGNAL),
        # 分岐 2
        ("2 signal/no", g(SIG), NO_PATCH, OUT_REVIEW_NO_PATCH_SIGNAL),
        ("2 low_conf/no", g(LOW), NO_PATCH, OUT_REVIEW_NO_PATCH_SIGNAL),
        # 分岐 3: vacant (msg-328 F1(b)); no stage-2 confidence is read
        # 分岐 4
        ("4 findings", g(SIG), patch(0.1, 0.75), OUT_FINDINGS_ONLY),
        ("4 non-mixed low_conf x high claim", g(LOW), patch(0.1, 0.9), OUT_FINDINGS_ONLY),
        ("4 no stage-1 x high claim", g(NS), patch(0.1, 0.9), OUT_FINDINGS_ONLY),
        # 分岐 5
        ("5 behaviour", g(SIG), patch(0.26, 0.1), OUT_REVIEW),
        ("5 claim", g(EX), patch(0.1, 0.5), OUT_REVIEW),
        # 分岐 6
        ("6 no stage-1 signal", g(NS), SAFE, OUT_REVIEW),
        ("6 low_conf", g(LOW), SAFE, OUT_REVIEW),
        # 分岐 7
        ("7 signal", g(SIG), SAFE, OUT_AUTO),
        ("7 boundary", g(SIG), patch(0.25, 0.25), OUT_AUTO),
        ("7 exempt", g(EX), SAFE, OUT_AUTO),
    ],
)
def test_route_patch(name, stage1, stage2, expected) -> None:
    assert route_patch(stage1, stage2, T) == expected, name


def test_route_patch_default_denies_an_unknown_stage1_class() -> None:
    # 分岐 8: a signal class without a recorded drift confidence cannot satisfy 分岐 7
    assert route_patch(GroupStage1(CLASS_SIGNAL, None), SAFE, T) == OUT_REVIEW


# ---------------------------------------------------------------- PR label


@pytest.mark.parametrize(
    ("outcomes", "label"),
    [
        ([OUT_AUTO, OUT_AUTO], LABEL_AUTO),
        ([OUT_AUTO, None], None),  # a draft with no applicable judgement keeps the PR unlabelled
        ([OUT_AUTO, OUT_NO_SIGNAL], None),
        ([OUT_AUTO, OUT_REVIEW], LABEL_REVIEW),
        ([None, OUT_REVIEW_MIXED_GROUP], LABEL_REVIEW),
        ([OUT_REVIEW_NO_PATCH_SIGNAL], LABEL_REVIEW),
        ([OUT_AUTO, OUT_FINDINGS_ONLY], LABEL_AUTO),  # findings_only drafts are not in the PR
        ([OUT_FINDINGS_ONLY], None),
        ([], None),
    ],
)
def test_pr_label(outcomes, label) -> None:
    assert pr_label(outcomes) == label


# ------------------------------------------- DoD 4'' (msg-329 / msg-330 boundaries)


@pytest.mark.parametrize(
    ("name", "stage1", "stage2", "expected"),
    [
        # the mid band (ruling condition 1): never auto, never silently dropped
        ("mid 0.5/0.5", g(SIG), patch(0.5, 0.5), OUT_REVIEW),
        ("mid 0.5/0.5 exempt", g(EX), patch(0.5, 0.5), OUT_REVIEW),
        # auto_pr_max_risk = 0.25: just under / equal / just over, on each question
        ("behaviour 0.24", g(SIG), patch(0.24, 0.1), OUT_AUTO),
        ("behaviour 0.25", g(SIG), patch(0.25, 0.1), OUT_AUTO),
        ("behaviour 0.26", g(SIG), patch(0.26, 0.1), OUT_REVIEW),
        ("claim 0.24", g(SIG), patch(0.1, 0.24), OUT_AUTO),
        ("claim 0.25", g(SIG), patch(0.1, 0.25), OUT_AUTO),
        ("claim 0.26", g(SIG), patch(0.1, 0.26), OUT_REVIEW),
        # findings_only_min_claim_risk = 0.75: just under / equal / just over
        ("claim 0.74", g(SIG), patch(0.1, 0.74), OUT_REVIEW),
        ("claim 0.75", g(SIG), patch(0.1, 0.75), OUT_FINDINGS_ONLY),
        ("claim 0.76", g(SIG), patch(0.1, 0.76), OUT_FINDINGS_ONLY),
        # a high behaviour risk does not stop findings_only
        ("behaviour 0.9 x claim 0.8", g(SIG), patch(0.9, 0.8), OUT_FINDINGS_ONLY),
        # non-mixed, stage-1 low_conf, claim 0.8: findings_only with no stage-2 confidence in play
        ("low_conf x claim 0.8", g(LOW), patch(0.1, 0.8), OUT_FINDINGS_ONLY),
        # low_conf stage 1 does not get auto even with a safe stage 2
        ("low_conf x safe", g(LOW), patch(0.25, 0.25), OUT_REVIEW),
        # mixed groups precede everything
        ("mixed x claim 0.8", g(SIG, LOW), patch(0.1, 0.8), OUT_REVIEW_MIXED_GROUP),
        ("mixed x safe", g(SIG, LOW), patch(0.1, 0.1), OUT_REVIEW_MIXED_GROUP),
    ],
)
def test_route_patch_boundaries(name, stage1, stage2, expected) -> None:
    assert route_patch(stage1, stage2, T) == expected, name


def test_stage2_has_no_confidence_field() -> None:
    # 分岐 3 is vacant: the stage-2 judgement carries probabilities only
    assert "confidence" not in PatchRiskJudgement.__dataclass_fields__
