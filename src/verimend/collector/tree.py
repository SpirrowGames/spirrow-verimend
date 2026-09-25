"""Read-only view of a checked-out target repository.

Every extractor walks the repository through this class, so they all agree on
one thing that matters for correctness: *which files count as reality*.

- Only files tracked by git are considered (``git ls-files``). Build output,
  virtualenvs, and local data a developer happens to have lying around are not
  part of the product the documentation describes.
- Test code is excluded. A test's fake tool, fake port, or fake environment
  variable is not a fact about the running product, and letting it in would
  make the reconciler "verify" documentation against fixtures.

A file that cannot be decoded or parsed is recorded in ``skipped`` rather than
raised: one malformed file must not cost the whole repository its facts.
"""

from __future__ import annotations

import ast
import subprocess
from functools import cached_property
from pathlib import Path, PurePosixPath

# Directory names whose contents are test code, not product reality.
EXCLUDED_DIRS = frozenset({"tests", "test", "testing", "fixtures"})

# Files larger than this are generated or vendored; reading them is not worth it.
MAX_FILE_BYTES = 1_000_000


class RepoTree:
    """The tracked, non-test files of a repository checkout."""

    def __init__(self, root: Path, files: list[str]) -> None:
        self.root = root
        self.files = tuple(
            sorted(f for f in files if not EXCLUDED_DIRS.intersection(PurePosixPath(f).parts[:-1]))
        )
        self.skipped: dict[str, str] = {}
        self._text: dict[str, str | None] = {}
        self._ast: dict[str, ast.Module | None] = {}

    @classmethod
    def from_checkout(cls, root: Path) -> "RepoTree":
        """Build the view from the files git tracks under ``root``."""
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            capture_output=True,
        ).stdout
        files = [f for f in out.decode("utf-8").split("\0") if f]
        return cls(root, files)

    @cached_property
    def python_files(self) -> tuple[str, ...]:
        return tuple(f for f in self.files if f.endswith(".py"))

    def matching(self, *patterns: str) -> tuple[str, ...]:
        """Files whose basename matches any of the glob ``patterns``."""
        return tuple(f for f in self.files if any(PurePosixPath(f).match(p) for p in patterns))

    def text(self, path: str) -> str | None:
        """The file decoded as UTF-8, or None (recorded in ``skipped``)."""
        if path not in self._text:
            self._text[path] = self._read(path)
        return self._text[path]

    def lines(self, path: str) -> list[str]:
        text = self.text(path)
        return text.splitlines() if text is not None else []

    def parse(self, path: str) -> ast.Module | None:
        """The file parsed as Python, or None (recorded in ``skipped``)."""
        if path not in self._ast:
            module = None
            text = self.text(path)
            if text is not None:
                try:
                    module = ast.parse(text, filename=path)
                except (SyntaxError, ValueError) as exc:
                    self.skipped[path] = f"unparsable: {exc.__class__.__name__}: {exc}"
            self._ast[path] = module
        return self._ast[path]

    def _read(self, path: str) -> str | None:
        full = self.root / path
        try:
            if full.is_symlink() or not full.is_file():
                return None
            if full.stat().st_size > MAX_FILE_BYTES:
                self.skipped[path] = "too large"
                return None
            return full.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            self.skipped[path] = "not UTF-8"
        except OSError as exc:
            self.skipped[path] = f"unreadable: {exc}"
        return None
