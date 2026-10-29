"""Claim extraction from one document, chunk by chunk (docs/design.md 5.2).

An ``ExtractionSession`` lives for one run. It sends each chunk to the LLM,
validates the answer, and classifies every failure into one of the classes
below -- the table in design 5.2 is the specification, this is its code:

==================  ===============================  ==================  ==========================
class               which failures                   retry               on final failure
==================  ===============================  ==================  ==========================
configuration       ``lexora_url`` unset, HTTP 401,  none                ``ExtractorConfigError``:
                    HTTP 403, HTTP 404                                   the run is ``failed``
transient           timeout, connection error, 5xx,  once, after         chunk failure; three in a
                    429, 408                         ``Retry-After``     row across chunks aborts
                                                     (integer seconds,   the stage
                                                     429/503 only) or
                                                     2 s
transient, too      429 / 503 with ``Retry-After``   none                abort the stage at once
long to wait        over ``retry_after_max_s``
permanent           any other 4xx                    none                chunk failure; three 4xx
                                                                         in a row (no successful
                                                                         Lexora call between)
                                                                         abort the stage
                    ``oversized_line``               (LLM not called)    chunk failure
                    ``schema`` (malformed output)    once, with the      chunk failure
                                                     violations
==================  ===============================  ==================  ==========================

The HTTP classification and the retry live in ``verimend.llm_policy``, shared
with the reconciler's LLM judge.

After an abort every remaining chunk of the run is failed as
``extractor_aborted`` without calling the LLM.

``strict`` is the evaluation mode. There, a transient failure says nothing
about the extractor's quality, so scoring it would understate recall: the
first transient chunk failure (after its retry) and any abort (a
permanent-HTTP streak included: it says the setup is broken) raise
``ExtractorAborted`` instead of being recorded. Permanent failures are the
extractor's own and are scored in both modes.

Output validation is in two layers:

- the *frame*: JSON, an array, each element exactly the five keys with the
  right types, a known ``claim_type``, ``anchor_start <= anchor_end`` and both
  inside the chunk. A frame violation that survives the retry fails the chunk
  (``schema``) and keeps none of its claims.
- the *subject*: ``verimend.claims.parse_subject``, and ``null`` for a type with
  no subject model. A violation here is retried along with the frame, but if
  it survives, the claim is kept with its raw ``subject_json`` -- the
  reconciler settles it as ``unverifiable`` / ``invalid_subject``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from verimend.claims import CLAIM_TYPES, SUBJECT_ADAPTERS, SubjectError, parse_subject
from verimend.collector.facts import canonical_json
from verimend.extractor.chunking import Chunk, chunk_document, chunk_hash
from verimend.extractor.client import LLMClient
from verimend.llm_policy import (  # noqa: F401 -- several are re-exported for callers and tests
    ABORT_CONSECUTIVE,
    ABORT_PERMANENT_HTTP,
    ABORT_PERMANENT_SCHEMA,
    ABORT_RETRY_AFTER,
    CONSECUTIVE_TRANSIENT_LIMIT,
    DEFAULT_WAIT_S,
    ERROR_TRANSPORT,
    ERROR_SCHEMA,
    ConfigStatus,
    Permanent,
    Streaks,
    Transient,
    Unwaitable,
    count_permanent,
    count_schema,
    parse_retry_after,
    request,
)

log = logging.getLogger(__name__)

# Bump when the output contract (the five keys, their meaning) changes. It is
# part of extractor_key, so a bump invalidates the incremental cache.
OUTPUT_SCHEMA_VERSION = 1
OUTPUT_KEYS = frozenset({"anchor_start", "anchor_end", "text", "claim_type", "subject"})

ERROR_OVERSIZED = "oversized_line"
ERROR_ABORTED = "extractor_aborted"


def load_prompt() -> str:
    return resources.files("verimend.extractor").joinpath("prompt.md").read_text(encoding="utf-8")


def extractor_key(prompt: str, tier: str) -> str:
    """SHA-256 over the prompt, the tier and the output schema version."""
    material = canonical_json({"prompt": prompt, "tier": tier, "schema": OUTPUT_SCHEMA_VERSION})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- exceptions


class ExtractorConfigError(Exception):
    """The environment is broken (no URL, rejected credentials). Never a partial result."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # lexora_url_unset | http_401 | http_403 | http_404


class ExtractorAborted(Exception):
    """Evaluation mode only: a transient failure or an abort happened, so no score may be computed."""

    def __init__(self, reason: str, detail: dict[str, Any]) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# ----------------------------------------------------------------- results


@dataclass(frozen=True)
class Document:
    repo: str
    path: str
    text: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExtractedClaim:
    anchor_start: int
    anchor_end: int
    text: str
    claim_type: str
    subject_json: str | None
    model: str | None


@dataclass(frozen=True)
class ChunkFailure:
    doc_repo: str
    doc_path: str
    lines: tuple[int, int]
    error: str

    def as_stats(self) -> dict[str, Any]:
        return {"doc_repo": self.doc_repo, "doc_path": self.doc_path, "lines": list(self.lines), "error": self.error}


@dataclass
class DocumentExtraction:
    document: Document
    chunks: list[Chunk]
    claims: list[ExtractedClaim] = field(default_factory=list)
    failures: list[ChunkFailure] = field(default_factory=list)

    @property
    def chunk_hash(self) -> str:
        return chunk_hash(self.chunks)


# ----------------------------------------------------------------- session


class ExtractionSession:
    """Extract claims for one run. Holds the cross-chunk state (consecutive failures, abort)."""

    def __init__(
        self,
        client: LLMClient,
        *,
        tier: str = "light",
        retry_after_max_s: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
        strict: bool = False,
        prompt: str | None = None,
    ) -> None:
        self.client = client
        self.tier = tier
        self.retry_after_max_s = retry_after_max_s
        self.sleep = sleep
        self.strict = strict
        self.prompt = prompt if prompt is not None else load_prompt()
        self.extractor_key = extractor_key(self.prompt, tier)
        self.streaks = Streaks()
        self.aborted: dict[str, Any] | None = None

    @property
    def consecutive_transient(self) -> int:
        return self.streaks.transient

    def close(self) -> None:
        """Release the client's resources (``LexoraClient``'s connection pool), if it holds any.

        The session owns its client: whoever ends the session closes it, so a
        long-running process that runs the pipeline repeatedly does not leak
        a pool per run.
        """
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "ExtractionSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def chunks(self, document: Document) -> list[Chunk]:
        return chunk_document(document.text)

    def extract(self, document: Document, chunks: list[Chunk] | None = None) -> DocumentExtraction:
        """Extract ``document``. Raises ``ExtractorConfigError``; in strict mode also ``ExtractorAborted``."""
        result = DocumentExtraction(document=document, chunks=chunks if chunks is not None else self.chunks(document))
        for chunk in result.chunks:
            self._extract_chunk(document, chunk, result)
        return result

    # -- one chunk

    def _extract_chunk(self, document: Document, chunk: Chunk, result: DocumentExtraction) -> None:
        def fail(error: str) -> None:
            result.failures.append(ChunkFailure(document.repo, document.path, chunk.span, error))

        if self.aborted is not None:
            fail(ERROR_ABORTED)
            return
        if chunk.oversized:
            log.error("%s:%s lines %s-%s: a single line exceeds the chunk limit", document.repo, document.path, *chunk.span)
            fail(ERROR_OVERSIZED)
            return

        at_chunk = {"doc_repo": document.repo, "doc_path": document.path, "lines": list(chunk.span)}
        try:
            claims = self._ask(document, chunk)
        except ConfigStatus as exc:
            raise ExtractorConfigError(exc.error) from exc
        except Unwaitable as exc:
            fail(exc.error)
            self._abort(ABORT_RETRY_AFTER, at_chunk, retry_after_s=exc.retry_after_s, cause=exc)
            return
        except Transient as exc:
            fail(exc.error)
            self.streaks.transient += 1
            log.warning("%s:%s lines %s-%s: transient failure %s (%d in a row)", document.repo, document.path,
                        *chunk.span, exc.error, self.streaks.transient)
            if self.strict:
                self._abort(exc.error, at_chunk, cause=exc)
            if self.streaks.transient >= CONSECUTIVE_TRANSIENT_LIMIT:
                self._abort(ABORT_CONSECUTIVE, at_chunk, cause=exc)
            return
        except Permanent as exc:
            log.error("%s:%s lines %s-%s: %s", document.repo, document.path, *chunk.span, exc.error)
            fail(exc.error)
            if count_permanent(self.streaks, exc):
                self._abort(ABORT_PERMANENT_HTTP, at_chunk, status=exc.status, cause=exc)
            elif count_schema(self.streaks, exc):
                self._abort(ABORT_PERMANENT_SCHEMA, at_chunk, cause=exc)
            return
        self.streaks.transient = 0
        self.streaks.permanent_schema = 0  # a well-formed response; see Streaks
        result.claims.extend(claims)

    def _abort(
        self, reason: str, at_chunk: dict[str, Any], *, cause: BaseException | None = None, **extra: Any
    ) -> None:
        """Stop the stage: ``{reason, at_chunk[, retry_after_s][, status]}``. Raises in strict mode.

        ``cause`` becomes the raised exception's ``__cause__`` (``raise ... from cause``); it is never put
        into ``detail``. With no cause the raise carries no ``from``, so an implicit ``__context__`` survives.
        """
        detail = {"reason": reason, "at_chunk": at_chunk, **{k: v for k, v in extra.items() if v is not None}}
        log.error("extraction aborted: %s", detail, exc_info=cause)  # traceback of the cause, if any
        if self.strict:
            if cause is not None:
                raise ExtractorAborted(reason, detail) from cause
            raise ExtractorAborted(reason, detail)
        self.aborted = detail

    def _ask(self, document: Document, chunk: Chunk) -> list[ExtractedClaim]:
        messages = [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": _render_chunk(document, chunk)},
        ]
        completion = self._request(messages)
        outcome = validate_output(completion.text, chunk)
        if outcome.frame_errors or outcome.subject_errors:
            messages += [
                {"role": "assistant", "content": completion.text or ""},
                {"role": "user", "content": _correction(outcome)},
            ]
            completion = self._request(messages)
            outcome = validate_output(completion.text, chunk)
            if outcome.frame_errors:
                log.error("%s:%s lines %s-%s: output still malformed after a retry: %s", document.repo,
                          document.path, *chunk.span, "; ".join(outcome.frame_errors[:5]))
                raise Permanent(ERROR_SCHEMA)
            for error in outcome.subject_errors:
                log.warning("%s:%s lines %s-%s: keeping a claim with an invalid subject: %s", document.repo,
                            document.path, *chunk.span, error)
        model = completion.model or self.tier
        return [
            ExtractedClaim(c["anchor_start"], c["anchor_end"], c["text"], c["claim_type"], c["subject_json"], model)
            for c in outcome.claims
        ]

    def _request(self, messages: list[dict[str, str]]):
        """One logical request under ``verimend.llm_policy`` (retry once on a transient failure)."""
        return request(
            self.client, messages, self.tier,
            retry_after_max_s=self.retry_after_max_s, sleep=self.sleep, streaks=self.streaks,
        )


# -------------------------------------------------------------- validation


@dataclass
class ValidationOutcome:
    claims: list[dict[str, Any]] = field(default_factory=list)
    frame_errors: list[str] = field(default_factory=list)
    subject_errors: list[str] = field(default_factory=list)


_FENCED = re.compile(r"^\s*```[A-Za-z]*\s*\n(.*)\n\s*```\s*$", re.DOTALL)


def validate_output(text: str | None, chunk: Chunk) -> ValidationOutcome:
    """Check the LLM's answer for one chunk against the frame and the subject rules."""
    out = ValidationOutcome()
    if text is None:
        out.frame_errors.append("the response carried no message content")
        return out
    fenced = _FENCED.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        out.frame_errors.append(f"not JSON: {exc}")
        return out
    if not isinstance(data, list):
        out.frame_errors.append(f"expected a JSON array, got {type(data).__name__}")
        return out

    for i, item in enumerate(data):
        where = f"item {i}"
        if not isinstance(item, dict):
            out.frame_errors.append(f"{where}: not an object")
            continue
        missing, extra = OUTPUT_KEYS - item.keys(), item.keys() - OUTPUT_KEYS
        if missing or extra:
            out.frame_errors.append(f"{where}: missing keys {sorted(missing)}, unexpected keys {sorted(extra)}")
            continue
        start, end = item["anchor_start"], item["anchor_end"]
        if not (_is_int(start) and _is_int(end)):
            out.frame_errors.append(f"{where}: anchor_start / anchor_end must be integers")
            continue
        if start > end:
            out.frame_errors.append(f"{where}: anchor_start {start} > anchor_end {end}")
            continue
        if start < chunk.start or end > chunk.end:
            out.frame_errors.append(f"{where}: anchor {start}-{end} is outside the chunk ({chunk.start}-{chunk.end})")
            continue
        if not isinstance(item["text"], str) or not item["text"].strip():
            out.frame_errors.append(f"{where}: text must be a non-empty string")
            continue
        claim_type = item["claim_type"]
        if claim_type not in CLAIM_TYPES:
            out.frame_errors.append(f"{where}: unknown claim_type {claim_type!r}")
            continue
        subject = item["subject"]
        if subject is not None and not isinstance(subject, dict):
            out.frame_errors.append(f"{where}: subject must be an object or null")
            continue

        subject_json = None if subject is None else canonical_json(subject)
        if claim_type in SUBJECT_ADAPTERS:
            try:
                parse_subject(claim_type, subject_json)
            except SubjectError as exc:
                out.subject_errors.append(f"{where} ({claim_type}): subject {exc}")
        elif subject is not None:
            out.subject_errors.append(f"{where} ({claim_type}): subject must be null for this claim_type")
        out.claims.append({**item, "subject_json": subject_json})
    return out


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _render_chunk(document: Document, chunk: Chunk) -> str:
    numbered = "\n".join(f"{n}: {line}" for n, line in zip(range(chunk.start, chunk.end + 1), chunk.lines))
    return f"Document {document.repo}/{document.path}, lines {chunk.start}-{chunk.end}:\n\n{numbered}"


def _correction(outcome: ValidationOutcome) -> str:
    problems = (outcome.frame_errors + outcome.subject_errors)[:10]
    return (
        "Your answer was rejected for these reasons:\n- "
        + "\n- ".join(problems)
        + "\nAnswer again with only the corrected JSON array."
    )
