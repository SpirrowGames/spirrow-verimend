"""Deterministic fact extractors (docs/design.md section 5.1). No LLM is involved.

Each extractor is a function ``RepoTree -> Iterable[Fact]``. ``REGISTRY`` maps
the names used in ``config/targets.yaml`` to them; a name that is valid in the
config but absent here is declared but not built yet, and the collector
reports it as skipped rather than failing the run.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from verimend.collector.extractors import config_keys, entrypoints, mcp_schema, ports
from verimend.collector.facts import Fact
from verimend.collector.tree import RepoTree
from verimend.targets import ExtractorName

Extractor = Callable[[RepoTree], Iterable[Fact]]

REGISTRY: dict[ExtractorName, Extractor] = {
    ExtractorName.MCP_SCHEMA: mcp_schema.extract,
    ExtractorName.CONFIG_KEYS: config_keys.extract,
    ExtractorName.ENTRYPOINTS: entrypoints.extract,
    ExtractorName.PORTS: ports.extract,
    # ExtractorName.SERVICE_HEALTH arrives with T04.
}

__all__ = ["REGISTRY", "Extractor"]
