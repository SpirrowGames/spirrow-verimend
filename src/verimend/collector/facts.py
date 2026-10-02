"""The unit every extractor produces, and how it is stored.

A fact is split in two on purpose:

- ``content`` is *what* is true (a tool and its parameters, an environment
  variable and its default, a port and the line it appears on). It is stored as
  canonical JSON and hashed, so the same fact yields the same ``content_hash``
  on every run and every host.
- the location (``path`` / ``line``) is *where* it was observed. It goes into
  ``source_ref`` only, so moving a tool definition twenty lines down does not
  turn it into a "new" fact.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

# Mirrors the CHECK constraint on fact.source_kind (migration 0001).
SourceKind = Literal["file", "tool_schema", "service_health", "config"]


@dataclass(frozen=True)
class Fact:
    source_kind: SourceKind
    path: str
    line: int | None
    content: dict[str, Any]

    def canonical_content(self) -> str:
        return canonical_json(self.content)

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_content().encode("utf-8")).hexdigest()

    def source_ref(self, repo: str, commit: str) -> str:
        """``owner/name@<commit>:<path>[:<line>]`` -- enough to open the exact line."""
        ref = f"{repo}@{commit}:{self.path}"
        return f"{ref}:{self.line}" if self.line is not None else ref


@dataclass(frozen=True)
class LiveFact:
    """A fact observed from a running system rather than read from a commit.

    Its location is fixed (``ref``) and names neither a repository nor a
    commit, because the state it records belongs to neither -- see
    ``verimend.collector.health``. It is stored through the same path as a
    ``Fact``, so ``source_ref`` takes the same arguments and ignores them.
    """

    source_kind: SourceKind
    ref: str
    content: dict[str, Any]

    def canonical_content(self) -> str:
        return canonical_json(self.content)

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_content().encode("utf-8")).hexdigest()

    def source_ref(self, repo: str, commit: str) -> str:
        return self.ref


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
