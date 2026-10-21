"""The golden-set loader, scorer and oracle (verimend/eval/golden.py, docs/design.md 8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from verimend.eval.golden import (
    GoldenCase,
    GoldenCaseError,
    Score,
    load_case,
    load_cases,
    run_oracle,
    score,
)

REPO = "SpirrowGames/spirrow-magickit"


# ------------------------------------------------------------ load_case


def _write_case(root: Path, *, doc: str | None = None, facts: list | None = None, gold: dict | None = None) -> Path:
    case = root / "case"
    case.mkdir(parents=True, exist_ok=True)
    (case / "doc.md").write_text(
        doc if doc is not None else "# T\n\n`run` takes `a`.\nIt listens on port 8113.\n", encoding="utf-8"
    )
    default_facts = [
        {
            "key": "tool:run",
            "source_kind": "tool_schema",
            "source_ref": f"{REPO}@abc:src/x.py:1",
            "content": {
                "extractor": "mcp_schema",
                "tool": "run",
                "parameters": [{"name": "a"}],
                "open_parameters": False,
            },
        },
        {
            "key": "port:8113",
            "source_kind": "file",
            "source_ref": f"{REPO}@abc:start.sh:3",
            "content": {"extractor": "ports", "port": 8113},
        },
    ]
    (case / "facts.json").write_text(json.dumps(facts if facts is not None else default_facts), encoding="utf-8")
    default_gold = {
        "claims": [
            {
                "id": "c1",
                "anchor": [3, 3],
                "claim_type": "parameter",
                "subject": {"polarity": "current", "tool": "run", "param": "a"},
                "final": "verified",
                "deterministic": "verified",
            },
            {
                "id": "c2",
                "anchor": [4, 4],
                "claim_type": "port",
                "subject": {"polarity": "current", "port": 8080},
                "final": "stale",
                "deterministic": "deferred",
                "evidence_keys": ["port:8113"],
            },
        ]
    }
    (case / "gold.json").write_text(json.dumps(gold if gold is not None else default_gold), encoding="utf-8")
    return case


def _gold_with(**changes: Any) -> dict:
    claim = {
        "id": "c1",
        "anchor": [3, 3],
        "claim_type": "parameter",
        "subject": {"polarity": "current", "tool": "run", "param": "a"},
        "final": "verified",
        "deterministic": "verified",
    }
    claim.update(changes)
    return {"claims": [claim]}


def test_load_case_accepts_a_valid_case(tmp_path: Path) -> None:
    case = load_case(_write_case(tmp_path))
    assert case.repo == REPO
    assert [c.id for c in case.claims] == ["c1", "c2"]


@pytest.mark.parametrize(
    ("gold", "message"),
    [
        (_gold_with(anchor=[3, 9]), "outside doc.md"),
        (_gold_with(evidence_keys=["tool:nope"]), "not in facts.json"),
        (_gold_with(final="stale", deterministic="stale"), "needs evidence_keys"),
        (_gold_with(subject={"polarity": "current", "tool": "run"}), "violates its model"),
        (_gold_with(subject=None), "needs a subject"),
        (_gold_with(claim_type="behavior"), "subject must be null"),
        (_gold_with(subject={"polarity": "negated", "tool": "run", "param": "a"}), "must be 'deferred'"),
        (_gold_with(deterministic="unverifiable"), "deterministic"),
        (_gold_with(final="deferred"), "final"),
        (_gold_with(final="verified", deterministic="stale", evidence_keys=["tool:run"]), "deterministically stale"),
        (
            {"claims": _gold_with()["claims"] * 2},
            "duplicate claim id",
        ),
        (
            {
                "claims": [
                    _gold_with()["claims"][0],
                    {**_gold_with()["claims"][0], "id": "c2", "subject": {"polarity": "current", "tool": "run", "param": "b"},
                     "deterministic": "deferred"},
                ]
            },
            "overlapping anchors",
        ),
        (
            {**_gold_with(), "injected_invalid": [{"anchor": [3, 3], "claim_type": "port",
                                                   "raw_subject_json": '{"polarity": "current", "port": 1}'}]},
            "tests nothing",
        ),
    ],
)
def test_load_case_rejects_a_broken_label(tmp_path: Path, gold: dict, message: str) -> None:
    with pytest.raises(GoldenCaseError, match=message):
        load_case(_write_case(tmp_path, gold=gold))


def test_load_case_rejects_facts_of_two_repositories(tmp_path: Path) -> None:
    facts = [
        {"key": "a", "source_kind": "file", "source_ref": f"{REPO}@abc:x:1", "content": {"extractor": "ports", "port": 1}},
        {"key": "b", "source_kind": "file", "source_ref": "o/other@abc:x:1", "content": {"extractor": "ports", "port": 2}},
    ]
    with pytest.raises(GoldenCaseError, match="one repository"):
        load_case(_write_case(tmp_path, facts=facts, gold=_gold_with(subject={"polarity": "current", "port": 1},
                                                                       claim_type="port")))


@pytest.mark.parametrize(
    ("facts", "message"),
    [
        ([], "facts.json is empty"),
        ({"key": "a"}, "not an array"),
        (
            [{"key": "a", "source_kind": "file", "source_ref": "no-repo-prefix", "content": {"extractor": "ports"}}],
            "one repository",
        ),
    ],
)
def test_load_case_rejects_unusable_facts(tmp_path: Path, facts: Any, message: str) -> None:
    with pytest.raises(GoldenCaseError, match=message):
        load_case(_write_case(tmp_path, facts=facts))


# --------------------------------------------------------------- score


@pytest.fixture
def case(tmp_path: Path) -> GoldenCase:
    return load_case(_write_case(tmp_path))


def _pred(anchor: list[int], claim_type: str, result: str | None, keys: list[str] | None = None) -> dict:
    return {"anchor": anchor, "claim_type": claim_type, "result": result, "evidence_fact_keys": keys or []}


def test_score_perfect_prediction(case: GoldenCase) -> None:
    s = score(case, [_pred([3, 3], "parameter", "verified"), _pred([4, 4], "port", "stale", ["port:8113"])], "final")
    assert (s.total.gold, s.total.matched, s.total.agree) == (2, 2, 2)
    assert (s.stale_precision, s.stale_recall, s.extraction_recall) == (1.0, 1.0, 1.0)
    assert s.mismatches == [] and s.extra == 0


def test_score_extra_extraction_and_non_claim_hit(tmp_path: Path) -> None:
    gold = _gold_with()
    gold["non_claims"] = [{"anchor": [4, 4], "reason": "plan"}]
    case = load_case(_write_case(tmp_path, gold=gold))
    s = score(
        case,
        [_pred([3, 3], "parameter", "verified"), _pred([4, 4], "port", "stale", ["port:8113"]),
         _pred([1, 1], "path", "verified")],
        "final",
    )
    assert s.extra == 2
    assert s.non_claim_hits == 1
    assert s.total.stale_fp == 1  # an extra stale is a false positive
    assert s.stale_precision == 0.0


def test_score_stale_with_the_wrong_evidence_is_wrong(case: GoldenCase) -> None:
    s = score(case, [_pred([3, 3], "parameter", "verified"), _pred([4, 4], "port", "stale", ["tool:run"])], "final")
    assert (s.total.stale_tp, s.total.stale_fp, s.total.stale_fn) == (0, 1, 1)
    assert [(m.claim_id, m.detail != "") for m in s.mismatches] == [("c2", True)]


def test_score_shifted_anchor_does_not_match(case: GoldenCase) -> None:
    s = score(case, [_pred([3, 3], "parameter", "verified"), _pred([1, 2], "port", "stale", ["port:8113"])], "final")
    assert s.total.matched == 1
    assert s.missed == [(case.id, "c2")]
    assert s.extra == 1
    assert (s.total.stale_fp, s.total.stale_fn) == (1, 1)


def test_score_overlapping_anchor_of_another_type_does_not_match(case: GoldenCase) -> None:
    s = score(case, [_pred([3, 4], "command", "verified")], "final")
    assert s.total.matched == 0 and s.extra == 1


def test_score_layer_changes_the_verdict_on_the_same_predictions(case: GoldenCase) -> None:
    preds = [_pred([3, 3], "parameter", "verified"), _pred([4, 4], "port", None)]
    deterministic = score(case, preds, "deterministic")
    final = score(case, preds, "final")
    assert deterministic.mismatches == [] and deterministic.total.agree == 2
    assert [(m.claim_id, m.expected, m.got) for m in final.mismatches] == [("c2", "stale", "deferred")]
    assert final.total.stale_fn == 1


def test_score_combine_refuses_mixed_layers(case: GoldenCase) -> None:
    with pytest.raises(ValueError):
        Score.combine([score(case, [], "final"), score(case, [], "deterministic")])


def test_score_combine_refuses_nothing() -> None:
    with pytest.raises(ValueError):
        Score.combine([])


def test_score_combine_sums_every_count_and_list(tmp_path: Path) -> None:
    gold = _gold_with()
    gold["claims"].append(
        {
            "id": "c2",
            "anchor": [4, 4],
            "claim_type": "port",
            "subject": {"polarity": "current", "port": 8080},
            "final": "stale",
            "deterministic": "deferred",
            "evidence_keys": ["port:8113"],
        }
    )
    gold["non_claims"] = [{"anchor": [1, 1], "reason": "heading"}]
    case = load_case(_write_case(tmp_path, gold=gold))
    perfect = score(case, [_pred([3, 3], "parameter", "verified"), _pred([4, 4], "port", "stale", ["port:8113"])], "final")
    flawed = score(
        case,
        [
            _pred([3, 3], "parameter", "stale", ["tool:run"]),  # wrong label: a parameter mismatch and a stale fp
            _pred([1, 1], "path", "verified"),  # extra, on the non-claim
        ],
        "final",
    )

    combined = Score.combine([perfect, flawed])

    assert combined.layer == "final"
    assert (combined.total.gold, combined.total.matched, combined.total.agree) == (4, 3, 2)
    assert (combined.total.stale_tp, combined.total.stale_fp, combined.total.stale_fn) == (1, 1, 1)
    assert (combined.stale_precision, combined.stale_recall, combined.extraction_recall) == (0.5, 0.5, 0.75)
    assert combined.by_type["parameter"].gold == 2 and combined.by_type["parameter"].agree == 1
    assert combined.by_type["parameter"].stale_fp == 1
    assert combined.by_type["port"].stale_tp == 1 and combined.by_type["port"].stale_fn == 1
    assert (combined.extra, combined.non_claim_hits) == (1, 1)
    assert [(m.claim_id, m.expected, m.got) for m in combined.mismatches] == [("c1", "verified", "stale")]
    assert combined.missed == [(case.id, "c2")]
    # The inputs are not modified.
    assert (perfect.total.gold, flawed.total.gold) == (2, 2)
    assert perfect.mismatches == [] and len(flawed.mismatches) == 1


# ------------------------------------------------------------ load_cases


def test_load_cases_reads_every_subdirectory_in_name_order_and_skips_files(tmp_path: Path) -> None:
    root = tmp_path / "golden"
    root.mkdir()
    for name in ("b-case", "a-case"):
        source = _write_case(tmp_path / f"src-{name}")
        source.rename(root / name)
    (root / "README.md").write_text("# not a case\n", encoding="utf-8")

    cases = load_cases(root)

    assert [c.id for c in cases] == ["a-case", "b-case"]
    assert all(len(c.claims) == 2 for c in cases)


def test_load_cases_fails_on_any_broken_case(tmp_path: Path) -> None:
    root = tmp_path / "golden"
    root.mkdir()
    _write_case(tmp_path / "src").rename(root / "a-case")
    (root / "b-case").mkdir()  # no doc.md / facts.json / gold.json
    with pytest.raises(GoldenCaseError, match="b-case"):
        load_cases(root)


# --------------------------------------------------------------- oracle


def test_run_oracle_scores_the_deterministic_layer_and_reports_injected(tmp_path: Path) -> None:
    gold = _gold_with()
    gold["claims"].append(
        {
            "id": "c2",
            "anchor": [4, 4],
            "claim_type": "port",
            "subject": {"polarity": "current", "port": 8080},
            "final": "stale",
            "deterministic": "deferred",
            "evidence_keys": ["port:8113"],
        }
    )
    gold["claims"].append(
        {
            "id": "c3",
            "anchor": [5, 5],
            "claim_type": "port",
            "subject": {"polarity": "current", "port": 8113},
            "final": "verified",
            "deterministic": "verified",
        }
    )
    gold["injected_invalid"] = [
        {"anchor": [3, 3], "claim_type": "parameter", "raw_subject_json": "{", "note": "broken"},
    ]
    doc = "# T\n\n`run` takes `a`.\nIt listens on port 8080.\nOr on port 8113.\n"
    case = load_case(_write_case(tmp_path, doc=doc, gold=gold))

    result = run_oracle(case)

    assert [p.result for p in result.predictions] == ["verified", None, "verified"]
    assert result.predictions[2].evidence_fact_keys == ["port:8113"]
    assert result.score.mismatches == [] and result.score.total.agree == 3
    assert [(i.result, i.error) for i in result.injected] == [("unverifiable", "subject_schema_violation")]
    assert result.totals["invalid_subject"] == 1
    assert result.totals["deferred"] == 1


def test_run_oracle_reports_a_stale_with_its_fact_keys(tmp_path: Path) -> None:
    gold = _gold_with(subject={"polarity": "current", "tool": "run", "param": "z"}, final="stale",
                      deterministic="stale", evidence_keys=["tool:run"])
    result = run_oracle(load_case(_write_case(tmp_path, gold=gold)))
    assert result.predictions[0].result == "stale"
    assert result.predictions[0].evidence_fact_keys == ["tool:run"]
    assert result.score.total.stale_tp == 1
