"""collector: reality, as deterministic facts (docs/design.md section 5.1)."""

from verimend.collector.facts import Fact
from verimend.collector.run import collect
from verimend.collector.tree import RepoTree

__all__ = ["Fact", "RepoTree", "collect"]
