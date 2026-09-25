"""``ports``: where a port number literally appears (docs/design.md 5.1).

One fact per (file, line, port). The line itself is kept as ``context``, since
"8114 appears here" is only useful to the reconciler next to what it is the
port *of*.

A bare integer is not a port, so a number is recorded only when its
surroundings say it is one. ``via`` names which rule matched:

Python (AST; bare string statements such as docstrings are skipped):
    ``assign``   assigned to a name containing ``port`` (``port = 8004``,
                 ``mcp_port: int = Field(default=8114)``, ``PORT = int(...)``)
    ``keyword``  passed as ``port=`` / ``*_port=``
    ``default``  default of a parameter named ``*port*``
    ``lookup``   the fallback of a lookup keyed by ``*port*``
                 (``getattr(s, "mcp_port", 8114)``, ``cfg.get("port", 8004)``)
    ``url`` / ``host_port``  inside a string literal, as below

Other text files (units, shell, YAML, TOML, INI, env, Dockerfile, compose):
    ``url``        ``scheme://host:PORT``
    ``host_port``  ``localhost:PORT`` / ``127.0.0.1:PORT``
    ``flag``       ``--port PORT`` / ``--port=PORT``
    ``key_value``  ``PORT=...`` / ``port: ...`` / ``mcp_port = ...``

Only 1024-65535 is considered: below that are well-known system ports, which
are not what a Spirrow service's documentation gets wrong. Markdown is never
read -- documentation is what gets checked, not what it gets checked against.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator

from verimend.collector.extractors._astutil import literal_str, terminal_name
from verimend.collector.facts import Fact
from verimend.collector.tree import RepoTree

MIN_PORT, MAX_PORT = 1024, 65535

TEXT_PATTERNS = (
    "*.service",
    "*.socket",
    "*.sh",
    "*.yaml",
    "*.yml",
    "*.toml",
    "*.ini",
    "*.cfg",
    "*.conf",
    "*.env",
    "*.env.*",
    ".env.*",
    "Dockerfile",
    "Dockerfile.*",
    "Caddyfile",
)

_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@'\"]+:(\d{4,5})\b", re.IGNORECASE)
_HOST_PORT = re.compile(r"\b(?:localhost|\d{1,3}(?:\.\d{1,3}){3}):(\d{4,5})\b")
# Whole words only: ``--transport`` and ``report:`` are not ports.
_FLAG = re.compile(r"--(?:[a-z0-9]+-)*port[=\s]+['\"]?(\d{4,5})\b", re.IGNORECASE)
_KEY_VALUE = re.compile(r"\b(?:[a-z0-9]+_)*ports?\b['\"]?\s*[=:]\s*['\"]?(\d{4,5})\b", re.IGNORECASE)

# Order matters: the first rule to claim a port on a line names its ``via``.
TEXT_RULES = (("url", _URL), ("host_port", _HOST_PORT), ("flag", _FLAG), ("key_value", _KEY_VALUE))
STRING_RULES = (("url", _URL), ("host_port", _HOST_PORT))

MAX_CONTEXT = 200


def extract(tree: RepoTree) -> Iterator[Fact]:
    text_files = frozenset(tree.matching(*TEXT_PATTERNS))
    for path in tree.files:
        if path.endswith(".py"):
            module = tree.parse(path)
            hits = _python_hits(module) if module is not None else []
        elif path in text_files:
            hits = _text_hits(tree.lines(path))
        else:
            continue
        yield from _facts(tree, path, hits)


def _facts(tree: RepoTree, path: str, hits: list[tuple[int, int, str]]) -> Iterator[Fact]:
    lines = tree.lines(path)
    seen: set[tuple[int, int]] = set()
    for line, port, via in sorted(hits):
        if (line, port) in seen:
            continue
        seen.add((line, port))
        context = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        yield Fact(
            source_kind="file",
            path=path,
            line=line,
            content={
                "extractor": "ports",
                "port": port,
                "via": via,
                "context": context[:MAX_CONTEXT],
            },
        )


def _text_hits(lines: list[str]) -> list[tuple[int, int, str]]:
    hits = []
    for number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            continue
        hits.extend((number, port, via) for port, via, _ in _scan(line, TEXT_RULES))
    return hits


def _scan(text: str, rules) -> list[tuple[int, str, int]]:
    """(port, via, offset of the match) for each distinct port in ``text``."""
    found: dict[int, tuple[str, int]] = {}
    for via, pattern in rules:
        for match in pattern.finditer(text):
            port = int(match.group(1))
            if MIN_PORT <= port <= MAX_PORT:
                found.setdefault(port, (via, match.start()))
    return [(port, via, offset) for port, (via, offset) in found.items()]


def _python_hits(module: ast.Module) -> list[tuple[int, int, str]]:
    hits: list[tuple[int, int, str]] = []

    def numbers(node: ast.expr | None, via: str) -> None:
        if node is None:
            return
        for sub in ast.walk(node):
            port = _port_value(sub)
            if port is not None:
                hits.append((sub.lineno, port, via))

    docstrings = {
        id(stmt.value)
        for stmt in ast.walk(module)
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
    }

    for node in ast.walk(module):
        if isinstance(node, ast.Assign) and any(_is_port_name(t) for t in node.targets):
            numbers(node.value, "assign")
        elif isinstance(node, ast.AnnAssign) and _is_port_name(node.target):
            numbers(node.value, "assign")
        elif isinstance(node, ast.keyword) and node.arg and _mentions_port(node.arg):
            numbers(node.value, "keyword")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
            for arg, default in [*zip(positional, defaults), *zip(args.kwonlyargs, args.kw_defaults)]:
                if _mentions_port(arg.arg):
                    numbers(default, "default")
        elif isinstance(node, ast.Call):
            # getattr(x, "mcp_port", 8114) / cfg.get("port", 8004): a port-named
            # key followed by its fallback.
            for i, arg in enumerate(node.args[:-1]):
                key = literal_str(arg)
                if key is not None and _mentions_port(key):
                    numbers(node.args[i + 1], "lookup")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            # A bare string statement (a docstring, typically) is prose about
            # the code, not configuration the code runs with.
            for port, via, offset in _scan(node.value, STRING_RULES):
                hits.append((node.lineno + node.value.count("\n", 0, offset), port, via))
    return hits


def _port_value(node: ast.AST) -> int | None:
    if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
        return None
    value = node.value
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, int) and MIN_PORT <= value <= MAX_PORT:
        return value
    return None


def _is_port_name(target: ast.expr) -> bool:
    name = terminal_name(target)
    return name is not None and _mentions_port(name)


_WORDS = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def _mentions_port(name: str) -> bool:
    """``port`` as a whole word of a snake_case / camelCase / UPPER name.

    A substring test would take ``transport``, ``report_interval`` and
    ``import_timeout`` for ports.
    """
    return any(word.lower() in ("port", "ports") for word in _WORDS.findall(name))
