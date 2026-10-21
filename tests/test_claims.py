"""The claim subject vocabulary (verimend/claims.py)."""

import pytest

from verimend.claims import (
    CLAIM_TYPES,
    CommandSubject,
    EnvParameterSubject,
    PortSubject,
    SubjectError,
    ToolParameterSubject,
    parse_subject,
)


def test_claim_types_are_the_eight_of_the_design() -> None:
    assert set(CLAIM_TYPES) == {"link", "version", "command", "parameter", "port", "path", "behavior", "architecture"}


@pytest.mark.parametrize(
    ("claim_type", "raw", "expected"),
    [
        ("port", '{"polarity": "current", "port": 8118}', PortSubject(polarity="current", port=8118)),
        (
            "port",
            '{"polarity": "negated", "port": 8080, "service": "verimend"}',
            PortSubject(polarity="negated", port=8080, service="verimend"),
        ),
        (
            "parameter",
            '{"polarity": "current", "tool": "research", "param": "query"}',
            ToolParameterSubject(polarity="current", tool="research", param="query"),
        ),
        (
            "parameter",
            '{"polarity": "historical", "env": "VERIMEND_DB_PATH"}',
            EnvParameterSubject(polarity="historical", env="VERIMEND_DB_PATH"),
        ),
        ("command", '{"polarity": "current", "command": "verimend migrate"}', CommandSubject(polarity="current", command="verimend migrate")),
    ],
)
def test_valid_subjects(claim_type, raw, expected) -> None:
    assert parse_subject(claim_type, raw) == expected


def test_null_subject_is_none() -> None:
    assert parse_subject("port", None) is None


def test_types_without_a_model_are_not_validated() -> None:
    assert parse_subject("behavior", '{"anything": 1}') is None


@pytest.mark.parametrize(
    ("claim_type", "raw"),
    [
        ("port", "{not json"),
        ("behavior", "{not json"),  # malformed JSON is a fault for every type
        ("port", '{"polarity": "current", "port": "8080"}'),  # strict: a string is not a port
        ("port", '{"polarity": "current", "port": 70000}'),
        ("port", '{"polarity": "current"}'),
        ("port", '{"polarity": "now", "port": 8080}'),
        ("port", '{"polarity": "current", "port": 8080, "host": "x"}'),  # unknown key
        ("port", '[8080]'),
        ("parameter", '{"polarity": "current", "tool": "t"}'),
        ("parameter", '{"polarity": "current", "tool": "t", "param": "p", "env": "E"}'),
        ("parameter", '{"polarity": "current", "env": ""}'),
        ("command", '{"polarity": "current", "command": ""}'),
        ("command", '{"command": "x"}'),
    ],
)
def test_invalid_subjects_raise(claim_type, raw) -> None:
    with pytest.raises(SubjectError):
        parse_subject(claim_type, raw)
