"""``mcp_schema``: FastMCP tool definitions, read from the AST (docs/design.md 5.1).

Two registration forms are recognised, because both occur in the target repos:

- decorator: ``@mcp.tool`` / ``@mcp.tool()`` / ``@mcp.tool(name=..., description=...)``
- call:      ``mcp.tool()(some_function)``

The receiver's name is not checked (``mcp``, ``server``, ``app`` ... are all in
use); the attribute being called ``tool`` is what identifies a registration.

The recorded parameters are the function signature as written -- names,
annotations and defaults as source text -- minus any parameter annotated as
FastMCP's ``Context``, which FastMCP injects and hides from the tool schema.
Nothing is imported or executed, so the facts are exactly what the code says,
even for a tool whose module would fail to import on this host.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from verimend.collector.extractors._astutil import (
    docstring_summary,
    keyword,
    literal_str,
    source_of,
    terminal_name,
)
from verimend.collector.facts import Fact
from verimend.collector.tree import RepoTree

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def extract(tree: RepoTree) -> Iterator[Fact]:
    for path in tree.python_files:
        module = tree.parse(path)
        if module is not None:
            yield from _tools_in_module(path, module)


def _tools_in_module(path: str, module: ast.Module) -> Iterator[Fact]:
    functions: dict[str, list[FunctionNode]] = {}
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, []).append(node)

    found: list[tuple[int, dict]] = []
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                registration = _registration(decorator)
                if registration is not None:
                    found.append((node.lineno, _describe(node.name, node, registration)))
        elif isinstance(node, ast.Call):
            # mcp.tool(...)(func): the outer call's callee is itself a tool(...) call.
            registration = _registration(node.func) if isinstance(node.func, ast.Call) else None
            if registration is not None and len(node.args) == 1 and isinstance(node.args[0], ast.Name):
                name = node.args[0].id
                candidates = functions.get(name, [])
                # An ambiguous or unresolvable reference still proves the tool
                # exists; its signature is recorded as unknown, not guessed.
                function = candidates[0] if len(candidates) == 1 else None
                found.append((node.lineno, _describe(name, function, registration)))

    for line, content in sorted(found, key=lambda item: item[0]):
        yield Fact(source_kind="tool_schema", path=path, line=line, content=content)


def _registration(node: ast.expr) -> ast.Attribute | ast.Call | None:
    """``node`` itself if it is ``<x>.tool`` or ``<x>.tool(...)``, else None."""
    callee = node.func if isinstance(node, ast.Call) else node
    if isinstance(callee, ast.Attribute) and callee.attr == "tool":
        return node
    return None


def _describe(function_name: str, function: FunctionNode | None, registration: ast.expr) -> dict:
    is_call = isinstance(registration, ast.Call)
    name_expr = keyword(registration, "name") if is_call else None
    description_expr = keyword(registration, "description") if is_call else None

    content: dict = {
        "extractor": "mcp_schema",
        "tool": literal_str(name_expr) or (function_name if name_expr is None else None),
        "function": function_name,
    }
    if name_expr is not None and literal_str(name_expr) is None:
        content["tool_expr"] = source_of(name_expr)

    if description_expr is not None:
        content["description_source"] = "argument"
        content["summary"] = _first_line(_leading_literal(description_expr))
    elif function is not None and docstring_summary(function):
        content["description_source"] = "docstring"
        content["summary"] = docstring_summary(function)
    else:
        content["description_source"] = None
        content["summary"] = None

    if function is None:
        content["parameters"] = None
        content["returns"] = None
    else:
        content["parameters"] = _parameters(function)
        content["returns"] = source_of(function.returns)
    return content


def _parameters(function: FunctionNode) -> list[dict]:
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    # Defaults align with the *last* positional parameters.
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    pairs = [*zip(positional, defaults), *zip(args.kwonlyargs, args.kw_defaults)]

    params = []
    for arg, default in pairs:
        if arg.arg in ("self", "cls") or _is_context(arg.annotation):
            continue
        params.append(
            {
                "name": arg.arg,
                "annotation": source_of(arg.annotation),
                "default": source_of(default),
                "required": default is None,
            }
        )
    return params


def _is_context(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return False
    if isinstance(annotation, ast.Subscript):  # Context[...] / Optional[Context]
        return _is_context(annotation.value) or _is_context(annotation.slice)
    if isinstance(annotation, ast.BinOp):  # Context | None
        return _is_context(annotation.left) or _is_context(annotation.right)
    return terminal_name(annotation) == "Context"


def _leading_literal(node: ast.expr) -> str | None:
    """The literal text a string expression starts with.

    ``"List ops. " + _CATALOG`` is not a constant, but its opening sentence is,
    and that sentence is what a summary needs.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _leading_literal(node.left)
    if isinstance(node, ast.JoinedStr):
        prefix = []
        for part in node.values:
            if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                break
            prefix.append(part.value)
        return "".join(prefix) or None
    return literal_str(node)


def _first_line(text: str | None) -> str | None:
    if text is None:
        return None
    return next((line.strip() for line in text.splitlines() if line.strip()), None)
