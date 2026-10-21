"""The golden set and its scorer (docs/design.md section 8).

A case is one directory under ``eval/golden/``:

``doc.md``      the document under evaluation
``facts.json``  a frozen snapshot of facts, ``[{key, source_kind, source_ref, content}]``.
                Frozen on purpose: facts read from a live repository would move
                under the labels and rot them.
``gold.json``   ``{claims, non_claims, injected_invalid}`` -- the labels

Every gold claim carries **two** labels, because the pipeline has two places
where an answer can be right:

``final``          ``verified`` / ``stale`` / ``unverifiable``: the true answer
                   against the frozen facts. What the whole pipeline (extractor,
                   deterministic checkers, LLM judge) should end with.
``deterministic``  ``verified`` / ``stale`` / ``deferred``: what the M2a
                   reconciler should return when handed the *correct* subject.
                   ``deferred`` is "no verdict row yet", not a verdict value
                   (``verdict.result`` has no such value), and it is the right
                   answer whenever the checker has no way to decide.

The two differ, legitimately: "do not use port 8000" is finally ``verified``
but deterministically ``deferred`` (a negated subject is never compared), and
"the MCP server defaults to port 8000" is finally ``stale`` but
deterministically ``verified`` (``port_literal`` is a weak match).

``unverifiable`` is never a deterministic label for a valid subject. The one
deterministic path to it is a subject that fails its schema, and those live in
``injected_invalid``: raw ``subject_json`` strings fed only to the oracle, each
expected to come back ``unverifiable`` and counted as ``invalid_subject``.

A broken *case* -- an unknown evidence key, an anchor past the end of the
document, a subject its own model rejects -- raises ``GoldenCaseError``. That
is an authoring mistake in the labels, not an extractor fault, so unlike the
reconciler it is not contained: an evaluation over a bad label is worthless.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from verimend.claims import SUBJECT_ADAPTERS, ClaimType, SubjectError, parse_subject
from verimend.collector.facts import SourceKind, canonical_json
from verimend.db.migrate import apply_migrations
from verimend.reconciler import reconcile
from verimend.reconciler.checkers import CHECKERS

GOLDEN_DIR = Path(__file__).resolve().parents[3] / "eval" / "golden"

FinalLabel = Literal["verified", "stale", "unverifiable"]
DeterministicLabel = Literal["verified", "stale", "deferred"]
Layer = Literal["deterministic", "final"]
Result = Literal["verified", "stale", "unverifiable", "deferred"]

DEFERRED = "deferred"
ORACLE_MODEL = "golden-oracle"

Line = Annotated[int, Field(ge=1)]


class GoldenCaseError(ValueError):
    """A golden case is malformed. The labels, not the pipeline, are wrong."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoldFact(_Model):
    key: Annotated[str, Field(min_length=1)]
    source_kind: SourceKind
    source_ref: Annotated[str, Field(min_length=1)]
    content: dict[str, Any]


class GoldClaim(_Model):
    id: Annotated[str, Field(min_length=1)]
    anchor: tuple[Line, Line]
    claim_type: ClaimType
    subject: dict[str, Any] | None
    final: FinalLabel
    deterministic: DeterministicLabel
    evidence_keys: list[str] = []
    note: str = ""


class NonClaim(_Model):
    anchor: tuple[Line, Line]
    reason: Annotated[str, Field(min_length=1)]


class InjectedInvalid(_Model):
    anchor: tuple[Line, Line]
    claim_type: ClaimType
    raw_subject_json: str
    note: str = ""


class _GoldFile(_Model):
    claims: Annotated[list[GoldClaim], Field(min_length=1)]
    non_claims: list[NonClaim] = []
    injected_invalid: list[InjectedInvalid] = []


class Prediction(_Model):
    """One claim the pipeline produced. ``result`` None means no verdict (= deferred)."""

    anchor: tuple[Line, Line]
    claim_type: ClaimType
    result: Result | None
    evidence_fact_keys: list[str] = []


@dataclass(frozen=True)
class GoldenCase:
    id: str
    path: Path
    doc_lines: tuple[str, ...]
    repo: str
    facts: tuple[GoldFact, ...]
    claims: tuple[GoldClaim, ...]
    non_claims: tuple[NonClaim, ...]
    injected_invalid: tuple[InjectedInvalid, ...]

    def text(self, anchor: tuple[int, int]) -> str:
        start, end = anchor
        return "\n".join(self.doc_lines[start - 1 : end])


# ---------------------------------------------------------------- loading


def load_case(path: Path | str) -> GoldenCase:
    """Read and validate one case directory. Raises ``GoldenCaseError``."""
    path = Path(path)
    try:
        doc = (path / "doc.md").read_text(encoding="utf-8")
        raw_facts = json.loads((path / "facts.json").read_text(encoding="utf-8"))
        raw_gold = json.loads((path / "gold.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldenCaseError(f"{path.name}: {exc}") from exc
    try:
        if not isinstance(raw_facts, list):
            raise GoldenCaseError(f"{path.name}: facts.json is not an array")
        facts = tuple(GoldFact.model_validate(f) for f in raw_facts)
        gold = _GoldFile.model_validate(raw_gold)
    except ValidationError as exc:
        raise GoldenCaseError(f"{path.name}: {exc}") from exc

    lines = tuple(doc.splitlines())
    case = GoldenCase(
        id=path.name,
        path=path,
        doc_lines=lines,
        repo=_repo_of(path.name, facts),
        facts=facts,
        claims=tuple(gold.claims),
        non_claims=tuple(gold.non_claims),
        injected_invalid=tuple(gold.injected_invalid),
    )
    _validate(case)
    return case


def load_cases(root: Path | str = GOLDEN_DIR) -> list[GoldenCase]:
    """Every case under ``root`` (each subdirectory is one), in name order.

    A missing ``root`` is an error, not an empty set: scoring zero cases
    would pass with every metric empty, so absent gold data fails loudly.
    """
    root = Path(root)
    if not root.is_dir():
        raise GoldenCaseError(f"{root}: golden set directory does not exist or is not a directory")
    return [load_case(d) for d in sorted(p for p in root.iterdir() if p.is_dir())]


def _repo_of(case_id: str, facts: Sequence[GoldFact]) -> str:
    if not facts:
        raise GoldenCaseError(f"{case_id}: facts.json is empty")
    repos = {f.source_ref.split("@", 1)[0] for f in facts if "@" in f.source_ref}
    if len(repos) != 1 or any("@" not in f.source_ref for f in facts):
        raise GoldenCaseError(f"{case_id}: every fact must have a source_ref '<repo>@...' of one repository, got {sorted(repos)}")
    return repos.pop()


def _validate(case: GoldenCase) -> None:
    def fail(message: str) -> None:
        raise GoldenCaseError(f"{case.id}: {message}")

    def check_anchor(what: str, anchor: tuple[int, int]) -> None:
        start, end = anchor
        if not start <= end <= len(case.doc_lines):
            fail(f"{what}: anchor {list(anchor)} outside doc.md (1..{len(case.doc_lines)})")

    keys = [f.key for f in case.facts]
    if len(keys) != len(set(keys)):
        fail("duplicate fact key")
    for f in case.facts:
        if not isinstance(f.content.get("extractor"), str):
            fail(f"fact {f.key!r}: content has no extractor")

    ids = [c.id for c in case.claims]
    if len(ids) != len(set(ids)):
        fail("duplicate claim id")

    for c in case.claims:
        where = f"claim {c.id}"
        check_anchor(where, c.anchor)
        polarity = None
        if c.claim_type in SUBJECT_ADAPTERS:
            if c.subject is None:
                fail(f"{where}: a {c.claim_type} claim needs a subject")
            try:
                polarity = parse_subject(c.claim_type, canonical_json(c.subject)).polarity  # type: ignore[union-attr]
            except SubjectError as exc:
                fail(f"{where}: subject violates its model: {exc}")
        elif c.subject is not None:
            fail(f"{where}: {c.claim_type} has no subject model yet, subject must be null")
        if (c.claim_type not in CHECKERS or polarity != "current") and c.deterministic != DEFERRED:
            fail(f"{where}: no checker compares this claim, deterministic must be 'deferred'")
        if c.deterministic == "stale" and c.final != "stale":
            fail(f"{where}: deterministically stale but finally {c.final}")
        if "stale" in (c.final, c.deterministic) and not c.evidence_keys:
            fail(f"{where}: a stale label needs evidence_keys")
        unknown = set(c.evidence_keys) - set(keys)
        if unknown:
            fail(f"{where}: evidence_keys not in facts.json: {sorted(unknown)}")

    for i, a in enumerate(case.claims):
        for b in case.claims[i + 1 :]:
            if a.claim_type == b.claim_type and _overlap(a.anchor, b.anchor):
                fail(f"claims {a.id} and {b.id}: same claim_type with overlapping anchors cannot be matched unambiguously")

    for n in case.non_claims:
        check_anchor("non_claim", n.anchor)
    for inj in case.injected_invalid:
        check_anchor("injected_invalid", inj.anchor)
        try:
            parse_subject(inj.claim_type, inj.raw_subject_json)
        except SubjectError:
            continue
        fail(f"injected_invalid {inj.note!r}: raw_subject_json is valid, so it tests nothing")


# ----------------------------------------------------------------- scoring


@dataclass
class Counts:
    gold: int = 0
    matched: int = 0
    agree: int = 0
    stale_tp: int = 0
    stale_fp: int = 0
    stale_fn: int = 0

    def add(self, other: "Counts") -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass(frozen=True)
class Mismatch:
    case_id: str
    claim_id: str
    claim_type: str
    expected: str
    got: str
    detail: str = ""


@dataclass
class Score:
    """Scores of one layer. Combine cases with ``Score.combine``.

    stale precision / recall are the primary metrics (section 8: the 90% bar).
    A predicted stale is a true positive only when it is matched to a gold
    stale claim *and* names at least one of its ``evidence_keys`` -- stale for
    the wrong reason is the hallucination 5.3 guards against, and counts as a
    false positive (and the gold claim as missed).
    """

    layer: Layer
    total: Counts = field(default_factory=Counts)
    by_type: dict[str, Counts] = field(default_factory=dict)
    extra: int = 0
    non_claim_hits: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)
    missed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def stale_precision(self) -> float | None:
        return _ratio(self.total.stale_tp, self.total.stale_tp + self.total.stale_fp)

    @property
    def stale_recall(self) -> float | None:
        return _ratio(self.total.stale_tp, self.total.stale_tp + self.total.stale_fn)

    @property
    def extraction_recall(self) -> float | None:
        return _ratio(self.total.matched, self.total.gold)

    def _type(self, claim_type: str) -> Counts:
        return self.by_type.setdefault(claim_type, Counts())

    @classmethod
    def combine(cls, scores: Iterable["Score"]) -> "Score":
        scores = list(scores)
        layers = {s.layer for s in scores}
        if len(layers) != 1:
            raise ValueError(f"cannot combine scores of layers {sorted(layers)}")
        out = cls(layer=layers.pop())
        for s in scores:
            out.total.add(s.total)
            for t, counts in s.by_type.items():
                out._type(t).add(counts)
            out.extra += s.extra
            out.non_claim_hits += s.non_claim_hits
            out.mismatches.extend(s.mismatches)
            out.missed.extend(s.missed)
        return out


def score(gold: GoldenCase, predicted: Sequence[Prediction | dict[str, Any]], layer: Layer) -> Score:
    """Score ``predicted`` against one case's labels of ``layer``.

    Matching: a prediction and a gold claim can pair only with the same
    ``claim_type`` and overlapping anchors; pairs are taken greedily by largest
    overlap (ties: document order), each side at most once. An unpaired
    prediction is an extra extraction; one overlapping a ``non_claims`` anchor
    is also counted in ``non_claim_hits``. Gold claims stay unscored by
    ``injected_invalid``, which only the oracle uses.

    A prediction without a verdict (``result`` None) is ``deferred``: right on
    the deterministic layer when that is the label, always wrong on the final
    layer, which has no such label.
    """
    preds = [p if isinstance(p, Prediction) else Prediction.model_validate(p) for p in predicted]
    out = Score(layer=layer)

    candidates = []
    for gi, g in enumerate(gold.claims):
        for pi, p in enumerate(preds):
            if g.claim_type == p.claim_type:
                overlap = _overlap(g.anchor, p.anchor)
                if overlap:
                    candidates.append((-overlap, gi, pi))
    pair: dict[int, int] = {}
    used: set[int] = set()
    for _, gi, pi in sorted(candidates):
        if gi not in pair and pi not in used:
            pair[gi] = pi
            used.add(pi)

    for gi, g in enumerate(gold.claims):
        expected = g.deterministic if layer == "deterministic" else g.final
        counts = Counts(gold=1)
        if gi not in pair:
            out.missed.append((gold.id, g.id))
            counts.stale_fn = int(expected == "stale")
        else:
            counts.matched = 1
            p = preds[pair[gi]]
            got = p.result or DEFERRED
            evidence_hit = bool(set(p.evidence_fact_keys) & set(g.evidence_keys))
            if got == "stale" and expected == "stale" and evidence_hit:
                counts.stale_tp = 1
            else:
                counts.stale_fp = int(got == "stale")
                counts.stale_fn = int(expected == "stale")
            if got == expected and (got != "stale" or evidence_hit):
                counts.agree = 1
            else:
                detail = "stale without any of the gold evidence_keys" if got == expected else ""
                out.mismatches.append(Mismatch(gold.id, g.id, g.claim_type, expected, got, detail))
        out.total.add(counts)
        out._type(g.claim_type).add(counts)

    for pi, p in enumerate(preds):
        if pi in used:
            continue
        out.extra += 1
        if any(_overlap(p.anchor, n.anchor) for n in gold.non_claims):
            out.non_claim_hits += 1
        if p.result == "stale":
            extra = Counts(stale_fp=1)
            out.total.add(extra)
            out._type(p.claim_type).add(extra)
    return out


# ------------------------------------------------------------------ oracle


@dataclass(frozen=True)
class InjectedOutcome:
    note: str
    result: str | None
    error: str | None


@dataclass(frozen=True)
class OracleResult:
    case_id: str
    score: Score
    predictions: tuple[Prediction, ...]
    injected: tuple[InjectedOutcome, ...]
    totals: dict[str, int]


def run_oracle(case: GoldenCase) -> OracleResult:
    """Measure the M2a deterministic stage as if the extractor were perfect.

    An in-memory database gets one ``crawl_run``, the case's facts, and a claim
    per gold claim carrying the *gold* subject, plus one per
    ``injected_invalid`` carrying its raw (broken) ``subject_json``. Then
    ``reconcile()`` runs, and the gold claims' outcomes are scored on the
    deterministic layer. The injected claims are reported separately: each
    should be ``unverifiable`` with ``subject_schema_violation``, and
    ``totals["invalid_subject"]`` should equal their number.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        apply_migrations(conn)
        run_id = conn.execute(
            "INSERT INTO crawl_run (started_at, status) VALUES ('1970-01-01T00:00:00+00:00', 'succeeded')"
        ).lastrowid
        key_of: dict[int, str] = {}
        for f in case.facts:
            content = canonical_json(f.content)
            fact_id = conn.execute(
                "INSERT INTO fact (run_id, source_kind, source_ref, content, content_hash) VALUES (?, ?, ?, ?, ?)",
                (run_id, f.source_kind, f.source_ref, content, hashlib.sha256(content.encode("utf-8")).hexdigest()),
            ).lastrowid
            key_of[fact_id] = f.key

        gold_ids = [
            _insert_claim(conn, run_id, case, c.anchor, c.claim_type, None if c.subject is None else canonical_json(c.subject))
            for c in case.claims
        ]
        injected_ids = [
            _insert_claim(conn, run_id, case, i.anchor, i.claim_type, i.raw_subject_json) for i in case.injected_invalid
        ]
        conn.commit()

        reconcile(conn, run_id)

        verdicts = {
            row[0]: (row[1], json.loads(row[2]))
            for row in conn.execute(
                "SELECT v.claim_id, v.result, v.evidence_json FROM verdict AS v JOIN claim AS c ON c.id = v.claim_id"
                " WHERE c.run_id = ?",
                (run_id,),
            )
        }
        predictions = []
        for claim_id, c in zip(gold_ids, case.claims):
            result, evidence = verdicts.get(claim_id, (None, {}))
            predictions.append(
                Prediction(
                    anchor=c.anchor,
                    claim_type=c.claim_type,
                    result=result,
                    evidence_fact_keys=[key_of[i] for i in evidence.get("fact_ids", [])],
                )
            )
        injected = []
        for claim_id, i in zip(injected_ids, case.injected_invalid):
            result, evidence = verdicts.get(claim_id, (None, {}))
            injected.append(InjectedOutcome(i.note, result, evidence.get("error")))
        (stats_json,) = conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run_id,)).fetchone()
        totals = json.loads(stats_json)["reconcile"]
    finally:
        conn.close()

    return OracleResult(
        case_id=case.id,
        score=score(case, predictions, "deterministic"),
        predictions=tuple(predictions),
        injected=tuple(injected),
        totals=totals,
    )


def _insert_claim(
    conn: sqlite3.Connection, run_id: int, case: GoldenCase, anchor: tuple[int, int], claim_type: str, subject_json: str | None
) -> int:
    return conn.execute(
        "INSERT INTO claim (run_id, doc_repo, doc_path, anchor_start, anchor_end, text, claim_type, extraction_model,"
        " subject_json) VALUES (?, ?, 'doc.md', ?, ?, ?, ?, ?, ?)",
        (run_id, case.repo, anchor[0], anchor[1], case.text(anchor), claim_type, ORACLE_MODEL, subject_json),
    ).lastrowid


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]) + 1)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None
