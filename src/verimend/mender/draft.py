"""The mender's first stage (docs/design.md 5.4, M3a): a patch draft for each stale verdict.

``mend_stale(conn, run_id, documents, mender)`` runs after the reconciler. It
writes ``verdict.patch_draft`` and nothing else: branches and pull requests are
``verimend.mender.publish`` (M3b). Every effect stays in the local database.

Targets: the run's verdicts with ``result = stale`` and ``patch_draft IS NULL``,
in verdict id order -- deterministic and LLM stales alike (an LLM stale with
no evidence was already downgraded by the judge). A verdict that already has
a draft is never touched again, so a re-run does not redraft it.

Per verdict, before any Lexora call:

1. **Evidence** (``no_evidence``). ``evidence_json`` must be a JSON object
   whose ``fact_ids`` is a non-empty list of integers, every one of them a
   fact of this run. Otherwise there is nothing to ground a draft on (the
   hallucination guard, design 5.4 / 10): no call, no draft, ``log.error``
   with the verdict id. Only this verdict is skipped; the stage goes on.
2. **Document** (``doc_changed``). The text is the one the extractor read in
   this run (the pipeline's ``documents``, from the same checkout). Its
   SHA-256 must equal the run's ``document.content_hash``; a document that is
   missing on either side counts the same way.
3. **Anchor** (``no_anchor``). ``anchor_start`` / ``anchor_end`` must be set
   and inside the document. The extractor guarantees this for the claims it
   writes; the check keeps a broken row from raising.

Then Lexora, tier ``mender_tier`` (design 6: Heavy), gets the claim, the
judgement's reason, the evidence facts and the anchor lines with
``CONTEXT_LINES`` lines of context, and answers ``{"lines": [...]}`` -- the
lines that replace the anchor range, nothing else. A frame violation is
retried once with the violations, like the other stages; if it survives the
verdict is a failure (``schema``).

**The diff is computed here, not by the model**: the document with the anchor
range replaced, against the document, as a unified diff. A change outside the
anchor range cannot be expressed, so design 5.4's "an out-of-range change is
rejected" holds by construction. The draft is rejected (no ``patch_draft``,
counted in ``stats.mend.rejected``) when the replacement is

- ``empty``: no lines, or only blank ones;
- ``no_change``: the same lines as the anchor range, ignoring trailing whitespace;
- ``runaway``: more than ``max(3 * anchor lines, 10)`` lines;
- ``structure``: the diff breaks the Markdown around it (``check_structure``).
  Its verdict ids are listed in ``stats.mend.rejected_structure`` so the PR
  body can name what was left out.

**Verdicts about the same lines are drafted together.** Stale verdicts whose
``(doc_repo, doc_path, anchor_start, anchor_end)`` are exactly equal form one
group: one Lexora call carries every claim of the group, and the one draft is
written to every verdict of it. Only exact equality groups -- an anchor that
merely overlaps another stays separate (publishing drops the loser as
``apply_conflict``), so a group never covers more lines than one anchor. A
group's outcome counts once per verdict in ``stats.mend`` (``drafted``,
``rejected``, ``failed``, ``not_sent``).

A draft is written and committed per verdict, so whatever stops the stage
later keeps it.

Lexora failures follow ``verimend.llm_policy`` with the verdict as the unit,
exactly as in the reconciler's LLM stage: configuration (401 / 403 / 404)
stops the stage (``stats.mend.config_error = {status, at_verdict}``); a
transient failure is retried once, then the verdict fails, three in a row
abort; an unwaitable Retry-After aborts at once; a permanent 4xx fails the
verdict, three in a row abort. After an abort the remaining verdicts are not
sent and ``stats.mend.aborted = {reason, at_verdict[, retry_after_s][, status]}``.
**Every failure of this stage makes the run ``partial``** (the pipeline),
never ``failed``: the extraction and the verdicts before it stay.

``strict`` raises ``MenderConfigError`` / ``MenderAborted`` instead, with the
same ``_abort`` contract as the other two stages (``raise ... from cause``,
the cause never stored in ``detail``).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from verimend.collector.facts import canonical_json
from verimend.extractor.client import LexoraClient, LLMClient
from verimend.extractor.extract import Document
from verimend.llm_policy import (
    ABORT_CONSECUTIVE,
    ABORT_PERMANENT_HTTP,
    ABORT_PERMANENT_SCHEMA,
    ABORT_RETRY_AFTER,
    CONSECUTIVE_TRANSIENT_LIMIT,
    ERROR_SCHEMA,
    ConfigStatus,
    Permanent,
    Streaks,
    Transient,
    Unwaitable,
    count_permanent,
    count_schema,
    request,
)

log = logging.getLogger(__name__)

CONTEXT_LINES = 3
FACT_CONTENT_MAX = 1500
RUNAWAY_FACTOR = 3
RUNAWAY_MIN_LINES = 10
OUTPUT_KEYS = frozenset({"lines"})


REJECT_EMPTY = "empty"
REJECT_NO_CHANGE = "no_change"
REJECT_RUNAWAY = "runaway"
REJECT_STRUCTURE = "structure"
REJECT_REASONS = (REJECT_EMPTY, REJECT_NO_CHANGE, REJECT_RUNAWAY, REJECT_STRUCTURE)

SKIP_DOC_CHANGED = "doc_changed"
SKIP_NO_EVIDENCE = "no_evidence"
SKIP_NO_ANCHOR = "no_anchor"
SKIP_REASONS = (SKIP_DOC_CHANGED, SKIP_NO_EVIDENCE, SKIP_NO_ANCHOR)


def load_prompt() -> str:
    return resources.files("verimend.mender").joinpath("prompt.md").read_text(encoding="utf-8")


# ------------------------------------------------------------- exceptions


class MenderConfigError(Exception):
    """Strict mode, or no ``lexora_url``: the setup is broken."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # lexora_url_unset | http_401 | http_403 | http_404


class MenderAborted(Exception):
    """Strict mode only: a transient failure or an abort happened."""

    def __init__(self, reason: str, detail: dict[str, Any]) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# ----------------------------------------------------------------- inputs


@dataclass(frozen=True)
class EvidenceFact:
    id: int
    kind: str
    content: Any


@dataclass(frozen=True)
class StaleToMend:
    """One stale verdict, with everything a draft needs already checked."""

    verdict_id: int
    claim_text: str
    claim_type: str
    reason: str
    doc_path: str
    lines: list[str]  # the whole document, line endings kept (``str.splitlines(keepends=True)``)
    anchor: tuple[int, int]  # 1-based, inclusive
    facts: list[EvidenceFact]

    @property
    def anchor_lines(self) -> list[str]:
        start, end = self.anchor
        return [_strip_eol(line) for line in self.lines[start - 1 : end]]


@dataclass(frozen=True)
class Draft:
    patch: str


@dataclass(frozen=True)
class Rejected:
    reason: str  # one of REJECT_REASONS


@dataclass(frozen=True)
class VerdictFailure:
    verdict_id: int
    error: str

    def as_stats(self) -> dict[str, Any]:
        return {"verdict_id": self.verdict_id, "error": self.error}


# ----------------------------------------------------------------- session


class MenderSession:
    """Draft patches for the stale verdicts of one run. Holds the cross-verdict state.

    Owns its client like ``JudgeSession``: close it, or use it as a context manager.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        tier: str = "heavy",
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
        self.streaks = Streaks()
        self.aborted: dict[str, Any] | None = None
        self.config_error: dict[str, Any] | None = None

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "MenderSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def stopped(self) -> bool:
        return self.aborted is not None or self.config_error is not None

    def draft(self, target: StaleToMend | Sequence[StaleToMend]) -> Draft | Rejected | VerdictFailure | None:
        """A draft, a rejection, a verdict failure, or None when the stage has stopped.

        ``target`` is one verdict or a group with the same document and anchor (``group_targets``):
        a group gets one call and one outcome, reported at its first verdict.
        Raises ``MenderConfigError`` / ``MenderAborted`` in strict mode.
        """
        group = [target] if isinstance(target, StaleToMend) else list(target)
        target = group[0]
        if self.stopped:
            return None
        try:
            lines = self._ask(group)
        except ConfigStatus as exc:
            log.error("verdict %s: mender configuration error %s; the stage stops", target.verdict_id, exc.error)
            if self.strict:
                raise MenderConfigError(exc.error) from exc
            self.config_error = {"status": exc.status, "at_verdict": target.verdict_id}
            return None
        except Unwaitable as exc:
            self._abort(ABORT_RETRY_AFTER, target.verdict_id, retry_after_s=exc.retry_after_s, cause=exc)
            return None
        except Transient as exc:
            self.streaks.transient += 1
            log.warning(
                "verdict %s: transient failure %s (%d in a row)", target.verdict_id, exc.error, self.streaks.transient
            )
            if self.strict:
                self._abort(exc.error, target.verdict_id, cause=exc)
            if self.streaks.transient >= CONSECUTIVE_TRANSIENT_LIMIT:
                self._abort(ABORT_CONSECUTIVE, target.verdict_id, cause=exc)
            return VerdictFailure(target.verdict_id, exc.error)
        except Permanent as exc:
            log.error("verdict %s: %s", target.verdict_id, exc.error)
            if count_permanent(self.streaks, exc):
                self._abort(ABORT_PERMANENT_HTTP, target.verdict_id, status=exc.status, cause=exc)
            elif count_schema(self.streaks, exc):
                self._abort(ABORT_PERMANENT_SCHEMA, target.verdict_id, cause=exc)
            return VerdictFailure(target.verdict_id, exc.error)
        self.streaks.transient = 0
        self.streaks.permanent_schema = 0  # a well-formed response; see Streaks
        rejection = check_replacement(target.anchor_lines, lines)
        if rejection is not None:
            log.warning("verdict %s: draft rejected (%s)", target.verdict_id, rejection)
            return Rejected(rejection)
        patch = build_patch(target.doc_path, target.lines, target.anchor, lines)
        broken = check_structure(patch)
        if broken is not None:
            log.warning("verdict %s: draft rejected (%s: %s)", target.verdict_id, REJECT_STRUCTURE, broken)
            return Rejected(REJECT_STRUCTURE)
        return Draft(patch)

    def _abort(self, reason: str, at_verdict: int, *, cause: BaseException | None = None, **extra: Any) -> None:
        """Stop the stage: ``{reason, at_verdict[, retry_after_s][, status]}``. Raises in strict mode.

        Same contract as the extractor's and the judge's ``_abort``: ``cause`` is chained with ``from``,
        its traceback is logged, and it is never stored.
        """
        detail = {"reason": reason, "at_verdict": at_verdict, **{k: v for k, v in extra.items() if v is not None}}
        log.error("mender aborted: %s", detail, exc_info=cause)
        if self.strict:
            if cause is not None:
                raise MenderAborted(reason, detail) from cause
            raise MenderAborted(reason, detail)
        self.aborted = detail

    def _ask(self, group: Sequence[StaleToMend]) -> list[str]:
        target = group[0]
        messages = [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": render_target(group)},
        ]
        completion = self._request(messages)
        lines, errors = validate_draft(completion.text)
        if errors:
            messages += [
                {"role": "assistant", "content": completion.text or ""},
                {"role": "user", "content": _correction(errors)},
            ]
            completion = self._request(messages)
            lines, errors = validate_draft(completion.text)
            if errors:
                log.error("verdict %s: draft still malformed after a retry: %s", target.verdict_id, "; ".join(errors[:5]))
                raise Permanent(ERROR_SCHEMA)
        assert lines is not None
        return lines

    def _request(self, messages: list[dict[str, str]]):
        return request(
            self.client, messages, self.tier,
            retry_after_max_s=self.retry_after_max_s, sleep=self.sleep, streaks=self.streaks,
        )


def mender_from_settings(settings, **kwargs) -> MenderSession:
    """A ``MenderSession`` on Lexora as configured. Raises ``MenderConfigError`` without a URL."""
    if not settings.lexora_url:
        raise MenderConfigError("lexora_url_unset")
    client = LexoraClient(settings.lexora_url, settings.lexora_timeout_s)
    return MenderSession(
        client, tier=settings.mender_tier, retry_after_max_s=settings.lexora_retry_after_max_s, **kwargs
    )


# ------------------------------------------------------- output and guard


def validate_draft(text: str | None) -> tuple[list[str] | None, list[str]]:
    """``(lines, [])`` or ``(None, violations)``. An element holding line breaks is split at them."""
    if text is None:
        return None, ["the response carried no message content"]
    stripped = text.strip()
    if stripped.startswith("```"):
        fenced = stripped.splitlines()
        if len(fenced) >= 2 and fenced[-1].strip() == "```":
            stripped = "\n".join(fenced[1:-1])
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, [f"not JSON: {exc}"]
    if not isinstance(data, dict):
        return None, [f"expected a JSON object, got {type(data).__name__}"]
    missing, extra = OUTPUT_KEYS - data.keys(), data.keys() - OUTPUT_KEYS
    if missing or extra:
        return None, [f"missing keys {sorted(missing)}, unexpected keys {sorted(extra)}"]
    raw = data["lines"]
    if not isinstance(raw, list) or not all(isinstance(line, str) for line in raw):
        return None, ["lines must be an array of strings"]
    lines: list[str] = []
    for item in raw:
        lines.extend(item.splitlines() or [""])
    return lines, []


def check_replacement(original: Sequence[str], replacement: Sequence[str]) -> str | None:
    """The rejection reason for ``replacement`` of the anchor lines ``original``, or None."""
    if not any(line.strip() for line in replacement):
        return REJECT_EMPTY
    if [line.rstrip() for line in replacement] == [line.rstrip() for line in original]:
        return REJECT_NO_CHANGE
    if len(replacement) > max(RUNAWAY_FACTOR * len(original), RUNAWAY_MIN_LINES):
        return REJECT_RUNAWAY
    return None


def check_structure(patch: str) -> str | None:
    """The structure rule (``rule1`` .. ``rule4``) the unified diff ``patch`` breaks, or None.

    Read per hunk. In a removed run followed by an added run, lines are paired by
    position (pairs where either side is blank are left out) for rules 1 and 2.

    1. A rewritten line keeps its leading marks: indentation, ``>`` depth, list
       marker kind and heading level (``_marks``).
    2. A rewritten table row (``|`` first) keeps its number of unescaped ``|``.
       Backslash pairs are taken out first, so a ``|`` after an even number of
       backslashes is a column and one after an odd number is text.
    3. An added line is not a copy of an unchanged line of the hunk, both compared
       without their leading marks (``_text``). Only text holding a letter or a
       digit is compared: blank lines, ``---``, fences and ``},`` legitimately repeat.
    4. The fences (lines starting with three backticks or tildes after whitespace)
       the hunk adds minus those it removes is even: a hunk never leaves a code
       block open, and a whole block may still be added or removed.
    """
    for context, removed, added, pairs in _hunks(patch):
        for old, new in pairs:
            if _marks(old) != _marks(new):
                return "rule1"
            if old.lstrip().startswith("|") and _columns(old) != _columns(new):
                return "rule2"
        unchanged = {_text(line) for line in context}
        if any(_counts_as_text(_text(line)) and _text(line) in unchanged for line in added):
            return "rule3"
        if (sum(map(_is_fence, added)) - sum(map(_is_fence, removed))) % 2:
            return "rule4"
    return None


_LEAD = re.compile(r"([ \t]*)((?:>[ \t]?)*)([ \t]*)")
_MARK = re.compile(r"(#{1,6}|[-*+]|\d+[.)])(?=[ \t]|$)")
_PIPE = re.compile(r"(?<!\\)\|")


def _marks(line: str) -> tuple[str, int, str, str | None]:
    """Indentation, ``>`` depth, indentation inside the quote, and the list or heading marker kind."""
    lead = _LEAD.match(line)
    assert lead is not None  # every part of it is optional
    mark = _MARK.match(line, lead.end())
    kind = None
    if mark is not None:
        token = mark.group(1)
        kind = f"ordered{token[-1]}" if token[0].isdigit() else token
    return lead.group(1), lead.group(2).count(">"), lead.group(3), kind


def _text(line: str) -> str:
    """``line`` without its leading marks and surrounding whitespace."""
    lead = _LEAD.match(line)
    assert lead is not None
    mark = _MARK.match(line, lead.end())
    return line[mark.end() if mark is not None else lead.end() :].strip()


def _counts_as_text(text: str) -> bool:
    return any(char.isalnum() for char in text)


def _columns(line: str) -> int:
    return len(_PIPE.findall(line.replace("\\\\", "\0")))


def _is_fence(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("```") or stripped.startswith("~~~")


_Hunk = tuple[list[str], list[str], list[str], list[tuple[str, str]]]


def _hunks(patch: str) -> list[_Hunk]:
    """Per hunk of ``patch``: its context, removed and added lines, and the rewritten pairs."""
    hunks: list[_Hunk] = []
    block_removed: list[str] = []
    block_added: list[str] = []

    def close_block() -> None:
        if hunks:
            pairs = zip(block_removed, block_added)
            hunks[-1][3].extend((old, new) for old, new in pairs if old.strip() and new.strip())
        block_removed.clear()
        block_added.clear()

    for raw in patch.splitlines()[2:]:  # past the ---/+++ header
        if raw.startswith("@@"):
            close_block()
            hunks.append(([], [], [], []))
            continue
        if not hunks or raw.startswith("\\"):
            continue
        context, removed, added, _ = hunks[-1]
        kind, line = raw[:1], raw[1:]
        if kind == "-":
            if block_added:
                close_block()
            removed.append(line)
            block_removed.append(line)
        elif kind == "+":
            added.append(line)
            block_added.append(line)
        else:
            close_block()
            context.append(line)
    close_block()
    return hunks


def build_patch(path: str, lines: Sequence[str], anchor: tuple[int, int], replacement: Sequence[str]) -> str:
    """The unified diff of replacing the anchor range of ``lines`` with ``replacement``.

    Only the anchor range is replaced, so every changed line of the diff is inside it.
    The replacement takes the anchor's line endings: the first anchor line's for every
    line but the last, the last anchor line's for the last (so a file without a final
    newline keeps it that way).
    """
    start, end = anchor
    old = list(lines)
    eol = _eol(old[start - 1]) or "\n"
    last_eol = _eol(old[end - 1])
    new_segment = [line + eol for line in replacement[:-1]] + [replacement[-1] + last_eol]
    new = old[: start - 1] + new_segment + old[end:]
    out = []
    for line in difflib.unified_diff(old, new, f"a/{path}", f"b/{path}", n=CONTEXT_LINES):
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


EDIT_MARK = "EDIT"
KEEP_MARK = "KEEP"


def render_target(target: StaleToMend | Sequence[StaleToMend]) -> str:
    """The user message for one verdict, or for a group sharing one document and anchor."""
    group = [target] if isinstance(target, StaleToMend) else list(target)
    target = group[0]
    start, end = target.anchor
    first = max(1, start - CONTEXT_LINES)
    last = min(len(target.lines), end + CONTEXT_LINES)
    if len(group) == 1:
        out = [
            f"Stale claim ({target.claim_type}) from {target.doc_path}, lines {start}-{end}:",
            target.claim_text,
            "",
            f"Why it was judged stale: {target.reason}",
        ]
    else:
        out = [
            f"{len(group)} stale claims from {target.doc_path}, all about lines {start}-{end}."
            " One rewrite of the passage must correct every one of them:"
        ]
        for n, member in enumerate(group, 1):
            out += ["", f"{n}. ({member.claim_type}) {member.claim_text}", f"   Why it was judged stale: {member.reason}"]
    out += ["", "Evidence facts:"]
    facts = {f.id: f for member in group for f in member.facts}
    for f in (facts[i] for i in sorted(facts)):
        content = canonical_json(f.content)
        if len(content) > FACT_CONTENT_MAX:
            content = content[:FACT_CONTENT_MAX] + "...(truncated)"
        out.append(json.dumps({"fact_id": f.id, "kind": f.kind}) + " " + content)
    # The line marks are words that are not Markdown, so a passage line that itself
    # starts with `>` (a blockquote) cannot be mistaken for the mark (run 4, msg-284).
    out += ["", f"Passage (rewrite only the lines marked {EDIT_MARK}):"]
    for n in range(first, last + 1):
        mark = EDIT_MARK if start <= n <= end else KEEP_MARK
        out.append(f"{mark} {n:>4} | {_strip_eol(target.lines[n - 1])}")
    return "\n".join(out)


def _correction(errors: Sequence[str]) -> str:
    return (
        "Your answer was rejected for these reasons:\n- "
        + "\n- ".join(errors[:10])
        + "\nAnswer again with only the corrected JSON object."
    )


def _eol(line: str) -> str:
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return ending
    stripped = line.splitlines()
    return line[len(stripped[0]) :] if stripped else ""


def _strip_eol(line: str) -> str:
    return line[: len(line) - len(_eol(line))] if line else line


# -------------------------------------------------------------- the stage


@dataclass
class MendPass:
    """What one ``mend_stale`` call did -- its return value, for the caller.

    Every field counts this pass only. ``drafted`` is the drafts this pass wrote;
    ``stats.mend.drafted`` is a different number, the run's total counted from the
    table (``_record``), so it stays true across re-runs. The other ``stats.mend``
    keys are this pass's, copied from here.
    """

    drafted: int = 0
    rejected: dict[str, int] = field(default_factory=lambda: dict.fromkeys(REJECT_REASONS, 0))
    skipped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SKIP_REASONS, 0))
    failures: list[VerdictFailure] = field(default_factory=list)
    not_sent: int = 0  # targets left after the stage stopped
    rejected_structure: list[int] = field(default_factory=list)  # verdict ids, named in the PR body


def mend_stale(
    conn: sqlite3.Connection, run_id: int, documents: Sequence[Document], mender: MenderSession
) -> MendPass:
    """Draft a patch for every draft-less stale verdict of the run. Updates ``stats.mend``."""
    rows = conn.execute(
        """
        SELECT v.id, v.evidence_json, c.text, c.claim_type, c.doc_repo, c.doc_path, c.anchor_start, c.anchor_end
        FROM verdict AS v JOIN claim AS c ON c.id = v.claim_id
        WHERE c.run_id = ? AND v.result = 'stale' AND v.patch_draft IS NULL
        ORDER BY v.id
        """,
        (run_id,),
    ).fetchall()
    texts = {(d.repo, d.path): d for d in documents}
    passed = MendPass()
    try:
        targets: list[tuple[str, StaleToMend]] = []
        for verdict_id, evidence_json, text, claim_type, repo, path, start, end in rows:
            target = _target(conn, run_id, texts, verdict_id, evidence_json, text, claim_type, repo, path, start, end)
            if isinstance(target, str):
                passed.skipped[target] += 1
            else:
                targets.append((repo, target))
        for group in group_targets(targets):
            ids = [t.verdict_id for t in group]
            if mender.stopped:
                passed.not_sent += len(ids)
                continue
            outcome = mender.draft(group)
            if outcome is None:
                passed.not_sent += len(ids)
            elif isinstance(outcome, VerdictFailure):
                passed.failures += [VerdictFailure(i, outcome.error) for i in ids]
            elif isinstance(outcome, Rejected):
                passed.rejected[outcome.reason] += len(ids)
                if outcome.reason == REJECT_STRUCTURE:
                    passed.rejected_structure += ids
            else:
                conn.executemany(
                    "UPDATE verdict SET patch_draft = ? WHERE id = ? AND patch_draft IS NULL",
                    [(outcome.patch, i) for i in ids],
                )
                conn.commit()
                passed.drafted += len(ids)
    finally:
        _record(conn, run_id, passed, mender)
        conn.commit()
    return passed


def group_targets(targets: Sequence[tuple[str, StaleToMend]]) -> list[list[StaleToMend]]:
    """The ``(repo, target)`` pairs grouped by exactly equal repository, document and anchor.

    Groups come in the order of their first verdict, members in the given order. Anchors
    that only overlap are never chained into one group.
    """
    groups: dict[tuple[str, str, tuple[int, int]], list[StaleToMend]] = {}
    for repo, target in targets:
        groups.setdefault((repo, target.doc_path, target.anchor), []).append(target)
    return list(groups.values())


def _target(
    conn: sqlite3.Connection,
    run_id: int,
    texts: dict[tuple[str, str], Document],
    verdict_id: int,
    evidence_json: str | None,
    text: str,
    claim_type: str,
    repo: str,
    path: str,
    start: int | None,
    end: int | None,
) -> StaleToMend | str:
    """The checked input of one draft, or the ``SKIP_*`` reason it cannot have one."""
    evidence = _evidence(evidence_json)
    facts = _facts(conn, run_id, evidence["fact_ids"]) if evidence is not None else None
    if evidence is None or facts is None:
        log.error("verdict %s: stale without usable evidence facts; no draft", verdict_id)
        return SKIP_NO_EVIDENCE
    document = texts.get((repo, path))
    row = conn.execute(
        "SELECT content_hash FROM document WHERE run_id = ? AND doc_repo = ? AND doc_path = ?", (run_id, repo, path)
    ).fetchone()
    if document is None or row is None or row[0] != hashlib.sha256(document.text.encode("utf-8")).hexdigest():
        log.warning("verdict %s: %s:%s is not the text this run extracted from; no draft", verdict_id, repo, path)
        return SKIP_DOC_CHANGED
    lines = document.text.splitlines(keepends=True)
    if not (isinstance(start, int) and isinstance(end, int) and 1 <= start <= end <= len(lines)):
        log.error("verdict %s: anchor %s-%s is not inside %s:%s; no draft", verdict_id, start, end, repo, path)
        return SKIP_NO_ANCHOR
    reason = evidence.get("reason")
    return StaleToMend(
        verdict_id, text, claim_type, reason if isinstance(reason, str) else "", path, lines, (start, end), facts
    )


def _evidence(evidence_json: str | None) -> dict[str, Any] | None:
    """The evidence object when its ``fact_ids`` is a non-empty list of integers, else None."""
    if evidence_json is None:
        return None
    try:
        evidence = json.loads(evidence_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(evidence, dict):
        return None
    ids = evidence.get("fact_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        return None
    return evidence


def _facts(conn: sqlite3.Connection, run_id: int, ids: Sequence[int]) -> list[EvidenceFact] | None:
    """The facts ``ids`` of this run, in id order; None when any of them is not a fact of this run."""
    wanted = sorted(set(ids))
    marks = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"SELECT id, source_kind, content FROM fact WHERE run_id = ? AND id IN ({marks}) ORDER BY id",
        (run_id, *wanted),
    ).fetchall()
    if len(rows) != len(wanted):
        return None
    return [EvidenceFact(row_id, kind, json.loads(content)) for row_id, kind, content in rows]


def _record(conn: sqlite3.Connection, run_id: int, passed: MendPass, mender: MenderSession) -> None:
    """``stats.mend``: the run's drafts counted from the table, this pass's outcomes and stop, and the run's PRs."""
    (drafted,) = conn.execute(
        """
        SELECT count(*) FROM verdict AS v JOIN claim AS c ON c.id = v.claim_id
        WHERE c.run_id = ? AND v.patch_draft IS NOT NULL
        """,
        (run_id,),
    ).fetchone()
    (stats_json,) = conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run_id,)).fetchone()
    stats = json.loads(stats_json)
    mend: dict[str, Any] = {
        "drafted": drafted,
        "rejected": dict(passed.rejected),
        "skipped": dict(passed.skipped),
        "failed": [f.as_stats() for f in passed.failures],
        "not_sent": passed.not_sent,
        "rejected_structure": list(passed.rejected_structure),
    }
    if mender.aborted is not None:
        mend["aborted"] = mender.aborted
    if mender.config_error is not None:
        mend["config_error"] = mender.config_error
    if "prs" in stats.get("mend", {}):
        mend["prs"] = stats["mend"]["prs"]  # the idempotency record of what is already published
    stats["mend"] = mend
    conn.execute("UPDATE crawl_run SET stats_json = ? WHERE id = ?", (canonical_json(stats), run_id))


def mend_stage_failed(mend_stats: dict[str, Any]) -> bool:
    """True when the mender left work undone: a verdict failure, an abort, a config error or an unpublished repository."""
    return (
        bool(mend_stats.get("failed"))
        or "aborted" in mend_stats
        or "config_error" in mend_stats
        or any(
            "publish_error" in pr or pr.get("reason") == "no_base_commit" for pr in mend_stats.get("prs", {}).values()
        )
    )
