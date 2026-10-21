"""Claims and the structured subject the extractor attaches to them (docs/design.md 4, 5.3).

A claim's ``text`` is prose. Prose says "do not use port 8080" and "changed
from 8080 to 9000", and a pattern run over it cannot tell either of those from
"the service listens on 8080". So the deterministic checkers never read
``text``. They read ``subject_json``: what the extractor (LLM), having read the
sentence in context, states the claim is *about*.

``polarity`` carries the part of that context a checker needs:

``current``     the document asserts this is true now -- the only polarity a
                checker compares against the facts
``negated``     the document says this is *not* so ("do not use port 8080")
``historical``  the document describes a past state ("changed from 8080 ...")

The models here are the single definition of that vocabulary. The extractor's
output schema and the golden set's labels (T05) are written against them.

Only the three claim types that have a deterministic checker in M2a have a
subject model. A subject on any other type is not validated here.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

# Mirrors the CHECK constraint on claim.claim_type (migration 0002).
ClaimType = Literal["link", "version", "command", "parameter", "port", "path", "behavior", "architecture"]
CLAIM_TYPES: tuple[str, ...] = ClaimType.__args__  # type: ignore[attr-defined]

Polarity = Literal["current", "negated", "historical"]

NonEmpty = Annotated[str, Field(min_length=1)]


class _Subject(BaseModel):
    # strict: "8080" is not a port, it is an extractor that emitted the wrong type.
    # forbid: an unknown key is a schema the checker does not understand.
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    polarity: Polarity


class PortSubject(_Subject):
    port: Annotated[int, Field(ge=1, le=65535)]
    service: NonEmpty | None = None


class ToolParameterSubject(_Subject):
    tool: NonEmpty
    param: NonEmpty


class EnvParameterSubject(_Subject):
    env: NonEmpty


class CommandSubject(_Subject):
    command: NonEmpty


ParameterSubject = ToolParameterSubject | EnvParameterSubject
Subject = PortSubject | ToolParameterSubject | EnvParameterSubject | CommandSubject

SUBJECT_ADAPTERS: dict[str, TypeAdapter] = {
    "port": TypeAdapter(PortSubject),
    "parameter": TypeAdapter(Union[ToolParameterSubject, EnvParameterSubject]),
    "command": TypeAdapter(CommandSubject),
}


class SubjectError(ValueError):
    """``subject_json`` is present but is not valid JSON or violates its schema.

    That is an extractor fault, not an undecidable claim, and is never folded
    into "deferred" (see ``verimend.reconciler``).
    """


def parse_subject(claim_type: str, subject_json: str | None) -> Subject | None:
    """The subject of a claim, or None when the claim has none to check.

    None means: ``subject_json`` is NULL, or ``claim_type`` has no subject
    model. Malformed JSON -- for any claim type -- and a schema violation for a
    modelled type raise ``SubjectError``.
    """
    if subject_json is None:
        return None
    try:
        raw = json.loads(subject_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SubjectError(f"not JSON: {exc}") from exc
    adapter = SUBJECT_ADAPTERS.get(claim_type)
    if adapter is None:
        return None
    try:
        return adapter.validate_python(raw)
    except ValidationError as exc:
        raise SubjectError(_summarise(exc)) from exc


def _summarise(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors(include_url=False)[:5]:
        location = ".".join(str(p) for p in error["loc"]) or "<root>"
        parts.append(f"{location}: {error['msg']}")
    more = exc.error_count() - len(parts)
    return "; ".join(parts) + (f" (+{more} more)" if more > 0 else "")
