"""Deterministic checkers (docs/design.md 5.3). No LLM, no reading of claim text.

Each checker is a pure function ``(subject, facts) -> Decision | None``:

- it sees only the structured subject (``verimend.claims``) of a claim whose
  polarity is ``current`` -- the caller holds back every other claim;
- ``facts`` are the facts of the claim's own repository in the claim's own run;
- ``None`` means "cannot be decided deterministically": no verdict is written
  and the claim is left for the LLM stage.

**A checker returns ``stale`` only when a fact that contradicts the claim
exists** (the hallucination guard of 5.3, applied from the deterministic stage
on). "No fact mentions it" is never stale: an extractor that failed stores no
facts at all (``collector/run.py``), so absence proves nothing. Whenever the
facts are incomplete about the subject, the answer is ``None``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from verimend.claims import (
    CommandSubject,
    EnvParameterSubject,
    PortSubject,
    Subject,
    ToolParameterSubject,
)


@dataclass(frozen=True)
class FactRow:
    id: int
    content: dict[str, Any]

    @property
    def extractor(self) -> str | None:
        return self.content.get("extractor")


@dataclass(frozen=True)
class Decision:
    result: Literal["verified", "stale"]
    fact_ids: tuple[int, ...]
    reason: str
    match: str | None = field(default=None)

    def evidence(self, checker: str) -> dict[str, Any]:
        evidence: dict[str, Any] = {"checker": checker, "fact_ids": list(self.fact_ids), "reason": self.reason}
        if self.match is not None:
            evidence["match"] = self.match
        return evidence


Checker = Callable[[Any, Sequence[FactRow]], "Decision | None"]


def check_port(subject: PortSubject, facts: Sequence[FactRow]) -> Decision | None:
    """verified when the port literally appears in the repository; else undecided.

    The match is weak: a ``ports`` fact says the number appears somewhere, not
    that it is the port of ``subject.service``. ``match="port_literal"`` marks
    that, so the golden set (T05) can measure what it costs. A missing port is
    not stale -- ports read from the environment or another repository leave no
    literal here.
    """
    hits = [f for f in facts if f.extractor == "ports" and f.content.get("port") == subject.port]
    if not hits:
        return None
    return Decision(
        "verified",
        _ids(hits),
        f"port {subject.port} appears as a literal in {len(hits)} place(s)",
        match="port_literal",
    )


def check_parameter(subject: ToolParameterSubject | EnvParameterSubject, facts: Sequence[FactRow]) -> Decision | None:
    if isinstance(subject, EnvParameterSubject):
        return _check_env(subject, facts)
    return _check_tool_parameter(subject, facts)


def _check_tool_parameter(subject: ToolParameterSubject, facts: Sequence[FactRow]) -> Decision | None:
    """verified when a definition of the tool has the parameter.

    stale only when every one of these holds -- otherwise the tool's parameter
    list is not known to be complete, and the answer is undecided:

    - exactly one ``tool_schema`` fact defines the tool (two definitions of the
      same name: which one runs is not a static fact);
    - its ``parameters`` is not None (the registered function was resolved);
    - its ``open_parameters`` is exactly False (no ``*args`` / ``**kwargs``; a
      fact without the key predates it and counts as unknown);
    - it has no ``tool_expr`` (the name is a literal, not computed);
    - and the parameter is not among ``parameters``.
    """
    tools = [f for f in facts if f.extractor == "mcp_schema" and f.content.get("tool") == subject.tool]
    if not tools:
        return None

    having = [f for f in tools if subject.param in _parameter_names(f)]
    if having:
        return Decision("verified", _ids(having), f"tool {subject.tool!r} has parameter {subject.param!r}")

    if len(tools) != 1:
        return None
    tool = tools[0].content
    if tool.get("parameters") is None or tool.get("open_parameters") is not False or tool.get("tool_expr") is not None:
        return None
    return Decision(
        "stale",
        _ids(tools),
        f"tool {subject.tool!r} has a closed signature without parameter {subject.param!r}",
    )


def _check_env(subject: EnvParameterSubject, facts: Sequence[FactRow]) -> Decision | None:
    """verified when the variable is read; never stale.

    ``config_keys`` cannot see keys built at runtime, so an unread variable is
    not proof the documentation is wrong.
    """
    hits = [f for f in facts if f.extractor == "config_keys" and f.content.get("env") == subject.env]
    if not hits:
        return None
    return Decision("verified", _ids(hits), f"environment variable {subject.env!r} is read")


def check_command(subject: CommandSubject, facts: Sequence[FactRow]) -> Decision | None:
    """verified when the command is one the ``entrypoints`` facts declare; never stale.

    A command matches a fact when, whitespace normalised, it *is* the fact's
    command (``match="exact"``) or begins with it followed by arguments
    (``match="prefix"``: ``verimend collect --repo x`` against the console
    script ``verimend``). A prefix match proves the program exists, not that it
    accepts those arguments, and is marked so.
    """
    tokens = subject.command.split()
    exact, prefix = [], []
    for fact in facts:
        if fact.extractor != "entrypoints":
            continue
        for declared in _declared_commands(fact.content):
            declared_tokens = declared.split()
            if not declared_tokens:
                continue
            if tokens == declared_tokens:
                exact.append(fact)
                break
            if tokens[: len(declared_tokens)] == declared_tokens:
                prefix.append(fact)
                break
    if exact:
        return Decision("verified", _ids(exact), f"{subject.command!r} is a declared entry point", match="exact")
    if prefix:
        return Decision(
            "verified", _ids(prefix), f"{subject.command!r} starts with a declared entry point", match="prefix"
        )
    return None


CHECKERS: dict[str, Checker] = {
    "port": check_port,
    "parameter": check_parameter,
    "command": check_command,
}


def check(claim_type: str, subject: Subject, facts: Sequence[FactRow]) -> Decision | None:
    checker = CHECKERS.get(claim_type)
    return checker(subject, facts) if checker is not None else None


def _declared_commands(content: dict[str, Any]) -> list[str]:
    kind = content.get("kind")
    if kind in ("console_script", "gui_script"):
        return [content["name"]] if content.get("name") else []
    if kind in ("python_module", "systemd_exec"):
        return [content["command"]] if content.get("command") else []
    return []


def _parameter_names(fact: FactRow) -> set[str]:
    return {p.get("name") for p in fact.content.get("parameters") or []}


def _ids(facts: Sequence[FactRow]) -> tuple[int, ...]:
    return tuple(sorted(f.id for f in facts))


def load_fact(row_id: int, content: str) -> FactRow:
    return FactRow(id=row_id, content=json.loads(content))
