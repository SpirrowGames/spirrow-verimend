"""T06 wiring: 判定 1 / 判定 2 inside mend_stale and publish (thread T-decide-drift-score).

Lexora is a fake ``DecideClient``; the mender LLM and ``gh`` are the fakes of
test_mender / test_mender_publish.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from extractor_fakes import FakeClient
from test_mender import TEXT, _docs, _draft, _fact, _mend_stats, _mender, _run, _stale, conn, lines  # noqa: F401
from test_mender_publish import A, add_draft, config, gh, make_run, remotes  # noqa: F401

from verimend.decide.lexora import POLICY_DRIFT, POLICY_PATCH_RISK, Decision
from verimend.decide.routing import Thresholds
from verimend.decide.stage import Decider, decider_from_settings, patch_sha256
from verimend.mender import mend_stage_failed, mend_stale, publish_prs
from verimend.mender.draft import mender_from_settings
from verimend.settings import Settings

FIX = lines("The service listens on port 8120.")


def drift_answer(level: float, conf: float = 0.9, mapping: float = 0.9) -> Decision:
    return Decision(
        True, "d-1", "jev",
        {"drift": {"score": level, "legend": ["a", "b", "c", "d"], "confidence": conf}, "mapping_correct": {"noul": mapping}},
    )


def patch_answer(behaviour: float, claim: float) -> Decision:
    return Decision(
        True, "p-1", "jev",
        {"changes_behavior_description": {"noul": behaviour}, "introduces_claim_not_in_code": {"noul": claim}},
    )


class FakeDecide:
    def __init__(self, drift: Decision | None = None, patch: Decision | None = None) -> None:
        self.answers = {POLICY_DRIFT: drift or drift_answer(2), POLICY_PATCH_RISK: patch or patch_answer(0.1, 0.1)}
        self.calls: list[str] = []

    def decide(self, *, policy, state, questions, questions_version) -> Decision:
        self.calls.append(policy)
        return self.answers[policy]


def decider(mode: str, client: FakeDecide) -> Decider:
    return Decider(client, mode, Thresholds(), now=lambda: "2026-11-22T00:00:00+00:00")


def rows(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT verdict_id, stage, mode, decision_id, patch_sha256, outcome FROM decision ORDER BY id"
    ).fetchall()]


# ------------------------------------------------------------- mend_stale


def test_off_builds_no_decider() -> None:
    assert decider_from_settings(Settings(lexora_url="http://l")) is None
    assert mender_from_settings(Settings(lexora_url="http://l")).decider is None
    assert isinstance(mender_from_settings(Settings(lexora_url="http://l", decide_mode="observe")).decider, Decider)


def test_observe_records_both_stages_and_changes_nothing(conn) -> None:
    run = _run(conn)
    verdict = _stale(conn, run, fact_ids=[_fact(conn, run)])
    client = FakeDecide(drift=drift_answer(0))  # would skip in enforce
    passed = mend_stale(conn, run, _docs(), _mender(FakeClient(FIX), decider=decider("observe", client)))
    assert passed.drafted == 1
    patch = _draft(conn, verdict)
    assert rows(conn) == [
        (verdict, "drift", "observe", "d-1", None, "skip_drift"),
        (verdict, "patch_risk", "observe", "p-1", patch_sha256(patch), "auto"),
    ]
    assert _mend_stats(conn, run)["decide"] == {"mode": "observe", "skipped": {"skip_drift": 0, "skip_mapping": 0}}


@pytest.mark.parametrize(
    ("answer", "outcome"), [(drift_answer(0), "skip_drift"), (drift_answer(2, mapping=0.1), "skip_mapping")]
)
def test_enforce_skips_on_a_confident_stage1_skip(conn, answer, outcome) -> None:
    run = _run(conn)
    verdict = _stale(conn, run, fact_ids=[_fact(conn, run)])
    llm = FakeClient(FIX)
    client = FakeDecide(drift=answer)
    passed = mend_stale(conn, run, _docs(), _mender(llm, decider=decider("enforce", client)))
    assert passed.drafted == 0 and _draft(conn, verdict) is None
    assert client.calls == [POLICY_DRIFT]  # no draft, so no 判定 2
    assert rows(conn) == [(verdict, "drift", "enforce", "d-1", None, outcome)]
    assert _mend_stats(conn, run)["decide"]["skipped"][outcome] == 1


@pytest.mark.parametrize("answer", [drift_answer(0, conf=0.3), Decision(False, error="http_500")])
def test_enforce_never_skips_on_low_confidence_or_no_signal(conn, answer) -> None:
    run = _run(conn)
    verdict = _stale(conn, run, fact_ids=[_fact(conn, run)])
    mend_stale(conn, run, _docs(), _mender(FakeClient(FIX), decider=decider("enforce", FakeDecide(drift=answer))))
    assert _draft(conn, verdict) is not None


def test_a_deterministic_verdict_is_exempt_without_a_call(conn) -> None:
    run = _run(conn)
    verdict = _stale(conn, run, fact_ids=[_fact(conn, run)], method="deterministic")
    client = FakeDecide()
    mend_stale(conn, run, _docs(), _mender(FakeClient(FIX), decider=decider("enforce", client)))
    assert client.calls == [POLICY_PATCH_RISK]
    assert [r[5] for r in rows(conn)] == ["exempt", "auto"]
    assert rows(conn)[0][3] is None


def test_the_patch_row_and_the_draft_are_one_transaction(conn) -> None:
    run = _run(conn)
    verdict = _stale(conn, run, fact_ids=[_fact(conn, run)])

    class Boom(Decider):
        def insert_patch_rows(self, conn, ids, outcome):
            raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        mend_stale(conn, run, _docs(), _mender(FakeClient(FIX), decider=Boom(FakeDecide(), "enforce", Thresholds())))
    conn.rollback()
    assert _draft(conn, verdict) is None
    assert [r[1] for r in rows(conn)] == ["drift"]


def test_a_group_with_mixed_stage1_classes_is_review_mixed_group(conn) -> None:
    run = _run(conn)
    fact = _fact(conn, run)
    first = _stale(conn, run, fact_ids=[fact])
    second = _stale(conn, run, fact_ids=[fact], method="deterministic")  # same anchor: one group
    mend_stale(conn, run, _docs(), _mender(FakeClient(FIX), decider=decider("enforce", FakeDecide())))
    patch_rows = [r for r in rows(conn) if r[1] == "patch_risk"]
    assert {(r[0], r[5]) for r in patch_rows} == {(first, "review_mixed_group"), (second, "review_mixed_group")}


# ---------------------------------------------------------------- publish


def _row(conn, verdict: int, outcome: str, *, mode: str = "enforce", sha: str | None = None) -> None:
    if sha is None:
        (patch,) = conn.execute("SELECT patch_draft FROM verdict WHERE id = ?", (verdict,)).fetchone()
        sha = patch_sha256(patch)
    conn.execute(
        "INSERT INTO decision (verdict_id, stage, mode, decision_id, patch_sha256, outcome, created_at)"
        " VALUES (?, 'patch_risk', ?, 'p', ?, ?, 'now')",
        (verdict, mode, sha, outcome),
    )
    conn.commit()


def _body(record: dict[str, Any]) -> str:
    with open(record["body_path"], encoding="utf-8") as f:
        return f.read()


def test_dry_run_writes_the_label_on_the_first_line(conn, remotes, gh, tmp_path) -> None:
    run = make_run(conn, {A: {"commit": remotes.create(A)}})
    verdict = add_draft(conn, run, A)
    _row(conn, verdict, "review")
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path, mode="dry_run"))
    assert prs[A]["label"] == "verimend:review"
    assert _body(prs[A]).startswith("Label: verimend:review\n\n")


@pytest.mark.parametrize(
    "row", [{"mode": "observe"}, {"sha": "0" * 64}], ids=["observe-row", "other-patch"]
)
def test_rows_that_do_not_apply_change_nothing(conn, remotes, gh, tmp_path, row) -> None:
    run = make_run(conn, {A: {"commit": remotes.create(A)}})
    verdict = add_draft(conn, run, A)
    _row(conn, verdict, "findings_only", **row)
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path, mode="dry_run"))
    assert "label" not in prs[A] and "findings_only" not in prs[A]
    assert prs[A]["applied"] == [verdict]
    assert not _body(prs[A]).startswith("Label:")


def test_findings_only_stays_out_of_the_pr_and_is_reported(conn, remotes, gh, tmp_path) -> None:
    run = make_run(conn, {A: {"commit": remotes.create(A)}})
    kept = add_draft(conn, run, A, (3, 3))
    hallucinated = add_draft(conn, run, A, (9, 9), claim="the made-up claim")
    _row(conn, kept, "auto")
    _row(conn, hallucinated, "findings_only")
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A]["applied"] == [kept] and prs[A]["findings_only"] == [hallucinated]
    with open(prs[A]["findings_path"], encoding="utf-8") as f:
        report = f.read()
    assert "the made-up claim" in report and "README.md:9" in report
    stats = json.loads(conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run,)).fetchone()[0])
    assert stats["mend"]["findings_only"] == {A: [hallucinated]}
    edit = [c["args"] for c in gh.calls if c["args"][:2] == ["pr", "edit"]]
    assert edit and edit[0][-2:] == ["--add-label", "verimend:auto"]
    assert prs[A]["label"] == "verimend:auto" and not mend_stage_failed(stats["mend"])


def test_all_findings_only_opens_no_pr(conn, remotes, gh, tmp_path) -> None:
    run = make_run(conn, {A: {"commit": remotes.create(A)}})
    verdict = add_draft(conn, run, A)
    _row(conn, verdict, "findings_only")
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A]["reason"] == "all_findings_only" and prs[A]["findings_only"] == [verdict]
    assert gh.calls == []


def test_findings_survive_a_missing_base_commit(conn, remotes, gh, tmp_path) -> None:
    remotes.create(A)
    run = make_run(conn, {A: {"error": "checkout failed"}})
    kept = add_draft(conn, run, A, (3, 3))
    hallucinated = add_draft(conn, run, A, (9, 9), claim="the made-up claim")
    _row(conn, kept, "auto")
    _row(conn, hallucinated, "findings_only")
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A]["reason"] == "no_base_commit"
    assert prs[A]["findings_only"] == [hallucinated]
    with open(prs[A]["findings_path"], encoding="utf-8") as f:
        assert "the made-up claim" in f.read()
    stats = json.loads(conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run,)).fetchone()[0])
    assert stats["mend"]["findings_only"] == {A: [hallucinated]}
    assert gh.calls == [] and mend_stage_failed(stats["mend"])


def test_a_label_failure_keeps_the_pr_and_makes_the_run_partial(conn, remotes, gh, tmp_path) -> None:
    run = make_run(conn, {A: {"commit": remotes.create(A)}})
    verdict = add_draft(conn, run, A)
    _row(conn, verdict, "review")

    real_calls = []

    def fail_edits():  # gh pr create succeeds, both pr edit attempts fail
        real_calls.append(1)

    cfg = config(remotes, gh, tmp_path)
    import verimend.mender.publish as publish

    original = publish._gh_add_label

    def failing(repo, url, label, config):
        fail_edits()
        gh.fail_next(1)
        original(repo, url, label, config)

    publish._gh_add_label, saved = failing, original
    try:
        prs = publish_prs(conn, run, cfg)
    finally:
        publish._gh_add_label = saved
    assert prs[A]["url"].startswith("https://") and "label_error" in prs[A]
    assert len(real_calls) == 2  # tried twice
    stats = json.loads(conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run,)).fetchone()[0])
    assert mend_stage_failed(stats["mend"])


def test_a_group_with_no_applicable_row_keeps_the_pr_unlabelled(conn, remotes, gh, tmp_path) -> None:
    run = make_run(conn, {A: {"commit": remotes.create(A)}})
    labelled = add_draft(conn, run, A, (3, 3))
    add_draft(conn, run, A, (9, 9))
    _row(conn, labelled, "auto")
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert "label" not in prs[A]
    assert not [c for c in gh.calls if c["args"][:2] == ["pr", "edit"]]
