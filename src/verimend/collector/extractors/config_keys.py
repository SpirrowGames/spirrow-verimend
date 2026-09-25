"""``config_keys``: configuration a service reads, from the AST (docs/design.md 5.1).

Two sources, each its own fact ``kind``:

``settings_field``
    A field of a pydantic-settings ``BaseSettings`` subclass, with the
    environment variable that sets it. A class counts as settings when one of
    its bases is ``BaseSettings`` or another settings class in the same
    repository (matched by name), and it inherits ``env_prefix`` from that
    base unless it declares its own. The environment variable follows
    pydantic-settings' rules: ``env_prefix + field`` upper-cased (unless
    ``case_sensitive=True``), or a literal ``alias`` / ``validation_alias``
    verbatim, which pydantic-settings does not prefix.

``env_read``
    A direct read with a literal key: ``os.environ[K]``, ``os.environ.get(K)``,
    ``os.getenv(K)``. ``required`` is true only for the subscript form, which
    raises when the variable is missing.

Keys built at runtime (f-strings, variables) are not facts this extractor can
state, and are left out rather than approximated.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field

from verimend.collector.extractors._astutil import keyword, literal_str, source_of, terminal_name
from verimend.collector.facts import Fact
from verimend.collector.tree import RepoTree

SETTINGS_BASE = "BaseSettings"
CONFIG_CALLS = frozenset({"SettingsConfigDict", "dict"})


@dataclass
class _SettingsClass:
    path: str
    node: ast.ClassDef
    bases: list[str]
    env_prefix: str | None  # None = not declared here; inherit
    case_sensitive: bool | None
    resolved_prefix: str = field(default="")
    resolved_case_sensitive: bool = field(default=False)


def extract(tree: RepoTree) -> Iterator[Fact]:
    classes: dict[str, list[_SettingsClass]] = {}
    for path in tree.python_files:
        module = tree.parse(path)
        if module is None:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.ClassDef):
                prefix, case_sensitive = _declared_config(node)
                bases = [b for b in (terminal_name(base) for base in node.bases) if b]
                classes.setdefault(node.name, []).append(
                    _SettingsClass(path, node, bases, prefix, case_sensitive)
                )

    for settings in _settings_classes(classes):
        yield from _fields(settings)

    for path in tree.python_files:
        module = tree.parse(path)
        if module is not None:
            yield from _env_reads(path, module)


def _settings_classes(classes: dict[str, list[_SettingsClass]]) -> list[_SettingsClass]:
    """Every class that is (transitively) a BaseSettings, with its config resolved."""
    resolved: dict[int, _SettingsClass] = {}

    def resolve(cls: _SettingsClass, seen: frozenset[int]) -> bool:
        if id(cls) in resolved:
            return True
        if id(cls) in seen:  # inheritance cycle by name; not settings
            return False
        parent: _SettingsClass | None = None
        is_settings = SETTINGS_BASE in cls.bases
        for base in cls.bases:
            for candidate in classes.get(base, []):
                if candidate is not cls and resolve(candidate, seen | {id(cls)}):
                    is_settings = True
                    parent = parent or candidate
        if not is_settings:
            return False
        cls.resolved_prefix = (
            cls.env_prefix if cls.env_prefix is not None else (parent.resolved_prefix if parent else "")
        )
        cls.resolved_case_sensitive = (
            cls.case_sensitive
            if cls.case_sensitive is not None
            else (parent.resolved_case_sensitive if parent else False)
        )
        resolved[id(cls)] = cls
        return True

    ordered = []
    for group in classes.values():
        for cls in group:
            if resolve(cls, frozenset()):
                ordered.append(cls)
    return sorted(ordered, key=lambda c: (c.path, c.node.lineno))


def _declared_config(node: ast.ClassDef) -> tuple[str | None, bool | None]:
    """``env_prefix`` / ``case_sensitive`` declared in this class body, if any."""
    prefix: str | None = None
    case_sensitive: bool | None = None
    for stmt in node.body:
        # model_config = SettingsConfigDict(env_prefix="X_") / {"env_prefix": "X_"}
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and _assigned_name(stmt) == "model_config":
            options = _config_options(stmt.value)
            prefix = literal_str(options.get("env_prefix"))
            case_sensitive = _literal_bool(options.get("case_sensitive"))
        # class Config: env_prefix = "X_"   (pydantic v1 style, still accepted)
        elif isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
            for inner in stmt.body:
                if isinstance(inner, ast.Assign) and _assigned_name(inner) == "env_prefix":
                    prefix = literal_str(inner.value)
                elif isinstance(inner, ast.Assign) and _assigned_name(inner) == "case_sensitive":
                    case_sensitive = _literal_bool(inner.value)
    return prefix, case_sensitive


def _config_options(node: ast.expr | None) -> dict[str, ast.expr]:
    """Keyword -> value of a ``SettingsConfigDict(...)`` / ``dict(...)`` / ``{...}`` literal."""
    if isinstance(node, ast.Call) and terminal_name(node.func) in CONFIG_CALLS:
        return {kw.arg: kw.value for kw in node.keywords if kw.arg}
    if isinstance(node, ast.Dict):
        return {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return {}


def _fields(settings: _SettingsClass) -> Iterator[Fact]:
    for stmt in settings.node.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        name = stmt.target.id
        if name.startswith("_") or name == "model_config" or _is_classvar(stmt.annotation):
            continue

        default, alias = _default_and_alias(stmt.value)
        if alias is not None:
            env = alias
        else:
            env = settings.resolved_prefix + name
            if not settings.resolved_case_sensitive:
                env = env.upper()

        yield Fact(
            source_kind="config",
            path=settings.path,
            line=stmt.lineno,
            content={
                "extractor": "config_keys",
                "kind": "settings_field",
                "class": settings.node.name,
                "field": name,
                "env": env,
                "annotation": source_of(stmt.annotation),
                "default": default,
            },
        )


def _default_and_alias(value: ast.expr | None) -> tuple[str | None, str | None]:
    """(default as source text, literal env alias) for a field's right-hand side."""
    if value is None:
        return None, None
    if isinstance(value, ast.Call) and terminal_name(value.func) == "Field":
        alias = literal_str(keyword(value, "validation_alias")) or literal_str(keyword(value, "alias"))
        factory = keyword(value, "default_factory")
        if factory is not None:
            return f"<factory: {source_of(factory)}>", alias
        explicit = keyword(value, "default")
        if explicit is None and value.args:
            explicit = value.args[0]
        if explicit is None or (isinstance(explicit, ast.Constant) and explicit.value is Ellipsis):
            return None, alias
        return source_of(explicit), alias
    return source_of(value), None


def _env_reads(path: str, module: ast.Module) -> Iterator[Fact]:
    for node in sorted(ast.walk(module), key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0))):
        key: str | None = None
        default: ast.expr | None = None
        required = False
        if isinstance(node, ast.Subscript) and _is_environ(node.value):
            if isinstance(node.ctx, ast.Load):
                key, required = literal_str(node.slice), True
        elif isinstance(node, ast.Call) and node.args and _is_env_getter(node.func):
            key = literal_str(node.args[0])
            default = node.args[1] if len(node.args) > 1 else keyword(node, "default")
        if key is None:
            continue
        yield Fact(
            source_kind="config",
            path=path,
            line=node.lineno,
            content={
                "extractor": "config_keys",
                "kind": "env_read",
                "env": key,
                "default": source_of(default),
                "required": required,
            },
        )


def _is_environ(node: ast.expr) -> bool:
    """``os.environ`` or a bare ``environ`` (from ``from os import environ``)."""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "environ"


def _is_env_getter(func: ast.expr) -> bool:
    """``os.environ.get`` / ``environ.get`` / ``os.getenv`` / ``getenv``."""
    if isinstance(func, ast.Attribute):
        if func.attr == "get":
            return _is_environ(func.value)
        return func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os"
    return isinstance(func, ast.Name) and func.id == "getenv"


def _assigned_name(stmt: ast.Assign | ast.AnnAssign) -> str | None:
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    if len(targets) == 1 and isinstance(targets[0], ast.Name):
        return targets[0].id
    return None


def _is_classvar(annotation: ast.expr) -> bool:
    node = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    return terminal_name(node) == "ClassVar"


def _literal_bool(node: ast.expr | None) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None
