"""T06 settings: decide_mode, value ranges, and the closed VERIMEND_DECIDE_ namespace."""

import pytest
from pydantic import ValidationError

from verimend.settings import Settings, unknown_decide_keys


@pytest.fixture(autouse=True)
def _clean_decide_env(monkeypatch) -> None:
    import os

    for key in list(os.environ):
        if key.upper().startswith("VERIMEND_DECIDE_"):
            monkeypatch.delenv(key)


def test_defaults_are_off_and_the_agreed_thresholds() -> None:
    s = Settings()
    assert s.decide_mode == "off"
    assert (
        s.decide_drift_min_level,
        s.decide_min_mapping_correct,
        s.decide_auto_pr_max_risk,
        s.decide_min_confidence,
        s.decide_findings_only_min_claim_risk,
    ) == (1, 0.40, 0.25, 0.60, 0.75)


def test_a_misspelled_decide_key_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("VERIMEND_DECIDE_MOD", "enforce")
    with pytest.raises(ValidationError, match="VERIMEND_DECIDE_MOD"):
        Settings()


def test_known_decide_keys_pass_and_other_namespaces_stay_lenient(monkeypatch) -> None:
    monkeypatch.setenv("VERIMEND_DECIDE_MODE", "observe")
    monkeypatch.setenv("VERIMEND_SOMETHING_ELSE", "x")
    assert Settings().decide_mode == "observe"
    assert unknown_decide_keys({"VERIMEND_DECIDE_MODE": "x", "VERIMEND_DECIDE_X": "y"}) == ["VERIMEND_DECIDE_X"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("VERIMEND_DECIDE_MODE", "on"),
        ("VERIMEND_DECIDE_DRIFT_MIN_LEVEL", "4"),
        ("VERIMEND_DECIDE_DRIFT_MIN_LEVEL", "-1"),
        ("VERIMEND_DECIDE_MIN_CONFIDENCE", "1.5"),
        ("VERIMEND_DECIDE_MIN_MAPPING_CORRECT", "-0.1"),
        ("VERIMEND_DECIDE_AUTO_PR_MAX_RISK", "0.75"),  # must stay below findings_only_min_claim_risk
        ("VERIMEND_DECIDE_FINDINGS_ONLY_MIN_CLAIM_RISK", "0.2"),
    ],
)
def test_out_of_range_values_are_rejected(monkeypatch, key, value) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings()
