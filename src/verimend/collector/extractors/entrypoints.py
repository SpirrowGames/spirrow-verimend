"""``entrypoints``: how the product is started (docs/design.md 5.1).

Three sources, each its own fact ``kind``:

``console_script``
    ``[project.scripts]`` / ``[project.gui-scripts]`` of every ``pyproject.toml``.

``systemd_exec``
    ``ExecStart=`` of every tracked ``*.service`` unit, with ``\\``
    continuation lines joined. Units shipped in a repository are frequently
    templates (``/path/to/...``); they are recorded as written, because a
    document that copies the template is describing exactly that text.

``python_module``
    A module that can be run directly: a ``__main__.py``, or a file with a
    top-level ``if __name__ == "__main__":``. When every directory from the
    import root (``src/`` if present, else the repository root) down to the
    file is a package, the command is ``python -m <module>``; otherwise it is
    ``python <path>``, since ``-m`` would not import it.
"""

from __future__ import annotations

import ast
import tomllib
from collections.abc import Iterator
from pathlib import PurePosixPath

from verimend.collector.facts import Fact
from verimend.collector.tree import RepoTree

SCRIPT_TABLES = {"scripts": "console_script", "gui-scripts": "gui_script"}


def extract(tree: RepoTree) -> Iterator[Fact]:
    yield from _pyproject_scripts(tree)
    yield from _systemd_units(tree)
    yield from _python_modules(tree)


def _pyproject_scripts(tree: RepoTree) -> Iterator[Fact]:
    for path in tree.matching("pyproject.toml"):
        text = tree.text(path)
        if text is None:
            continue
        try:
            project = tomllib.loads(text).get("project", {})
        except tomllib.TOMLDecodeError as exc:
            tree.skipped[path] = f"unparsable: {exc}"
            continue
        lines = text.splitlines()
        for table, kind in SCRIPT_TABLES.items():
            for name, target in sorted(project.get(table, {}).items()):
                yield Fact(
                    source_kind="config",
                    path=path,
                    line=_line_of_key(lines, name),
                    content={
                        "extractor": "entrypoints",
                        "kind": kind,
                        "name": name,
                        "target": target,
                    },
                )


def _systemd_units(tree: RepoTree) -> Iterator[Fact]:
    for path in tree.matching("*.service"):
        section = None
        logical: list[tuple[int, str]] = []
        pending: tuple[int, str] | None = None
        for number, raw in enumerate(tree.lines(path), start=1):
            if pending is not None:
                start, text = pending
                pending = None
                raw = text + " " + raw.strip()
                number = start
            line = raw.strip()
            if line.endswith("\\"):
                pending = (number, line[:-1].rstrip())
                continue
            logical.append((number, line))

        for number, line in logical:
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif section == "Service" and line.startswith("ExecStart="):
                yield Fact(
                    source_kind="config",
                    path=path,
                    line=number,
                    content={
                        "extractor": "entrypoints",
                        "kind": "systemd_exec",
                        "unit": PurePosixPath(path).name,
                        "command": line.removeprefix("ExecStart=").strip(),
                    },
                )


def _python_modules(tree: RepoTree) -> Iterator[Fact]:
    packages = {str(PurePosixPath(f).parent) for f in tree.matching("__init__.py")}
    for path in tree.python_files:
        pure = PurePosixPath(path)
        if pure.name == "__main__.py":
            line = 1
        else:
            module = tree.parse(path)
            line = _main_guard_line(module) if module is not None else None
            if line is None:
                continue

        module_name = _module_name(pure, packages)
        yield Fact(
            source_kind="config",
            path=path,
            line=line,
            content={
                "extractor": "entrypoints",
                "kind": "python_module",
                "module": module_name,
                "command": f"python -m {module_name}" if module_name else f"python {path}",
            },
        )


def _main_guard_line(module: ast.Module) -> int | None:
    for stmt in module.body:
        if (
            isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.Compare)
            and isinstance(stmt.test.left, ast.Name)
            and stmt.test.left.id == "__name__"
            and len(stmt.test.comparators) == 1
            and isinstance(stmt.test.comparators[0], ast.Constant)
            and stmt.test.comparators[0].value == "__main__"
        ):
            return stmt.lineno
    return None


def _module_name(path: PurePosixPath, packages: set[str]) -> str | None:
    """Dotted module for ``python -m``, or None when ``-m`` cannot import it."""
    parts = path.parts
    root = 1 if parts[0] == "src" and len(parts) > 1 else 0
    dirs = parts[root:-1]
    for depth in range(1, len(dirs) + 1):
        if str(PurePosixPath(*parts[: root + depth])) not in packages:
            return None
    if path.name == "__main__.py":
        return ".".join(dirs) or None
    return ".".join([*dirs, path.stem])


def _line_of_key(lines: list[str], key: str) -> int | None:
    """Best-effort line of ``key = ...`` in a TOML file (tomllib keeps no positions)."""
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        for candidate in (key, f'"{key}"', f"'{key}'"):
            if stripped.startswith(candidate) and stripped[len(candidate) :].lstrip().startswith("="):
                return number
    return None
