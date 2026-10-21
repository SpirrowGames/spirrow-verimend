"""The golden set data (eval/golden) checked against the T05 design and the M2a oracle."""

from __future__ import annotations

import json
from typing import Any

import pytest

from verimend.eval.golden import GOLDEN_DIR, GoldenCase, Score, load_cases, run_oracle

DETERMINISTIC_TYPES = {"port", "parameter", "command"}
LLM_TYPES = {"behavior", "architecture"}
LATER_CHECKER_TYPES = {"path", "link", "version"}


@pytest.fixture(scope="module")
def cases() -> list[GoldenCase]:
    return load_cases()


@pytest.fixture(scope="module")
def oracle(cases: list[GoldenCase]) -> dict[str, Any]:
    return {c.id: run_oracle(c) for c in cases}


# ------------------------------------------------------------ the data set


def test_golden_dir_is_the_repository_eval_golden() -> None:
    assert GOLDEN_DIR.is_dir()
    assert (GOLDEN_DIR / "README.md").is_file()


def test_every_case_loads_and_the_count_is_20_to_30(cases: list[GoldenCase]) -> None:
    case_dirs = [p for p in GOLDEN_DIR.iterdir() if p.is_dir()]
    assert len(cases) == len(case_dirs)
    assert 20 <= len(cases) <= 30


def test_docs_stay_within_one_chunk(cases: list[GoldenCase]) -> None:
    # 5.2 feeds the extractor heading-sized chunks of up to ~2000 tokens.
    # Whitespace-separated words are a conservative stand-in for tokens here.
    for case in cases:
        assert len(" ".join(case.doc_lines).split()) <= 2000, case.id


def _all_claims(cases: list[GoldenCase]):
    return [(case, claim) for case in cases for claim in case.claims]


def test_stale_composition_meets_the_minimums(cases: list[GoldenCase]) -> None:
    stale = [(case, c) for case, c in _all_claims(cases) if c.final == "stale"]
    assert len(stale) >= 25
    assert len({case.id for case, _ in stale}) >= 12
    for group in (DETERMINISTIC_TYPES, LLM_TYPES, LATER_CHECKER_TYPES):
        assert sum(c.claim_type in group for _, c in stale) >= 3, group


def test_control_cases_without_drift(cases: list[GoldenCase]) -> None:
    controls = [case for case in cases if all(c.final != "stale" for c in case.claims)]
    assert len(controls) >= 4


def _tool_facts(case: GoldenCase, tool: str) -> list[dict[str, Any]]:
    return [f.content for f in case.facts if f.content.get("extractor") == "mcp_schema" and f.content.get("tool") == tool]


def _trap(case: GoldenCase, claim) -> str | None:
    """Which trap of the T05 design a gold claim is, derived from the data."""
    subject = claim.subject or {}
    if subject.get("polarity") in ("negated", "historical"):
        return "negated_historical"
    if claim.claim_type == "parameter" and "tool" in subject:
        tools = _tool_facts(case, subject["tool"])
        if len(tools) >= 2:
            return "duplicate_tool"
        if tools and tools[0].get("open_parameters") and subject["param"] not in {p["name"] for p in tools[0]["parameters"]}:
            return "open_parameters"
    if claim.claim_type == "parameter" and "env" in subject:
        if not any(f.content.get("env") == subject["env"] for f in case.facts if f.content.get("extractor") == "config_keys"):
            return "env_mismatch"
    return None


def _traps(cases: list[GoldenCase]) -> dict[str, list[tuple[GoldenCase, Any]]]:
    out: dict[str, list] = {}
    for case, claim in _all_claims(cases):
        trap = _trap(case, claim)
        if trap is not None:
            out.setdefault(trap, []).append((case, claim))
    return out


def test_each_trap_appears_at_least_twice(cases: list[GoldenCase]) -> None:
    traps = _traps(cases)
    for name in ("negated_historical", "open_parameters", "duplicate_tool", "env_mismatch"):
        assert len(traps.get(name, [])) >= 2, name
    assert sum(len(case.non_claims) for case in cases) >= 2


def test_open_parameter_and_env_traps_are_deferred_then_unverifiable(cases: list[GoldenCase]) -> None:
    traps = _traps(cases)
    for _, claim in traps["open_parameters"] + traps["env_mismatch"]:
        assert (claim.deterministic, claim.final) == ("deferred", "unverifiable"), claim.id


def test_injected_invalid_has_every_kind_of_breakage(cases: list[GoldenCase]) -> None:
    injected = [i for case in cases for i in case.injected_invalid]
    assert len(injected) >= 3
    raws = [i.raw_subject_json for i in injected]

    def not_json(raw: str) -> bool:
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            return True
        return False

    assert any(not_json(r) for r in raws)
    assert any('"port": "' in r for r in raws)  # an int field given a string
    assert any('"args"' in r for r in raws)  # a key no model has


def test_readme_records_the_fact_commit() -> None:
    readme = (GOLDEN_DIR / "README.md").read_text(encoding="utf-8")
    shas = {
        f.source_ref.split("@", 1)[1].split(":", 1)[0]
        for case in load_cases()
        for f in case.facts
        if not f.source_ref.split("@", 1)[1].startswith("golden-injected")
    }
    assert len(shas) == 1
    assert shas.pop() in readme


# ------------------------------------------------------- the oracle (M2a)


def test_oracle_matches_every_deterministic_label(oracle: dict[str, Any]) -> None:
    combined = Score.combine(r.score for r in oracle.values())
    assert combined.mismatches == []
    assert combined.missed == []
    assert combined.extra == 0
    assert combined.total.agree == combined.total.gold


def test_oracle_stale_precision_is_perfect_on_deterministic_types(oracle: dict[str, Any]) -> None:
    combined = Score.combine(r.score for r in oracle.values())
    stale_tp = sum(combined.by_type[t].stale_tp for t in DETERMINISTIC_TYPES if t in combined.by_type)
    stale_fp = sum(combined.by_type[t].stale_fp for t in DETERMINISTIC_TYPES if t in combined.by_type)
    assert stale_tp > 0
    assert stale_fp == 0
    assert combined.stale_precision == 1.0


def test_oracle_never_calls_a_trap_stale(cases: list[GoldenCase], oracle: dict[str, Any]) -> None:
    for name, entries in _traps(cases).items():
        for case, claim in entries:
            index = case.claims.index(claim)
            assert oracle[case.id].predictions[index].result != "stale", (name, case.id, claim.id)


def test_oracle_settles_injected_subjects_as_unverifiable_not_deferred(
    cases: list[GoldenCase], oracle: dict[str, Any]
) -> None:
    for case in cases:
        result = oracle[case.id]
        assert all(i.result == "unverifiable" and i.error == "subject_schema_violation" for i in result.injected), case.id
        assert result.totals["invalid_subject"] == len(case.injected_invalid), case.id
        # Deferred counts exactly the gold claims labelled deferred: none of the injected ones.
        assert result.totals["deferred"] == sum(c.deterministic == "deferred" for c in case.claims), case.id
