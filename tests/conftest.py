"""Shared fixtures."""

import subprocess
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent

import pytest

from verimend.collector import RepoTree


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


def _write_repo(root: Path, files: dict[str, str]) -> str:
    """Write ``files`` (dedented) under ``root``, commit them, return the commit SHA."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(text).lstrip("\n"), encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def make_tree(tmp_path: Path) -> Callable[[dict[str, str]], RepoTree]:
    """Build a committed repository from ``{path: source}`` and view it as a RepoTree."""
    counter = iter(range(1000))

    def make(files: dict[str, str]) -> RepoTree:
        root = tmp_path / f"repo{next(counter)}"
        _write_repo(root, files)
        return RepoTree.from_checkout(root)

    return make


@pytest.fixture
def write_repo() -> Callable[[Path, dict[str, str]], str]:
    """Write ``{path: source}`` under a root, commit it, return the commit SHA."""
    return _write_repo
