"""Small AST helpers shared by the Python-reading extractors."""

from __future__ import annotations

import ast


def terminal_name(node: ast.expr) -> str | None:
    """``foo`` for ``foo``, ``bar`` for ``foo.bar`` -- else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def literal_str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def source_of(node: ast.expr | None) -> str | None:
    """The expression as source text, or None for no expression."""
    return ast.unparse(node) if node is not None else None


def keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def docstring_summary(node: ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef) -> str | None:
    """First non-empty line of the docstring."""
    doc = ast.get_docstring(node)
    if not doc:
        return None
    return next((line.strip() for line in doc.splitlines() if line.strip()), None)
