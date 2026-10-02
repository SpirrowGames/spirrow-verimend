"""Deterministic fact extractors (docs/design.md section 5.1). No LLM is involved.

Each extractor is a function ``RepoTree -> Iterable[Fact]``. ``REGISTRY`` maps
the names used in ``config/targets.yaml`` to the extractors that read the
checked-out tree. ``service_health`` is the one name absent from it: it reads a
running system, so it needs a ``HealthSource`` that only the caller can supply.
``extractors_for`` completes the map with it, and the collector resolves every
name through that complete map -- there is no "not built yet" path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from verimend.collector import health
from verimend.collector.extractors import config_keys, entrypoints, mcp_schema, ports
from verimend.collector.facts import Fact, LiveFact
from verimend.collector.tree import RepoTree
from verimend.targets import ExtractorName

Extractor = Callable[[RepoTree], Iterable[Fact | LiveFact]]

REGISTRY: dict[ExtractorName, Extractor] = {
    ExtractorName.MCP_SCHEMA: mcp_schema.extract,
    ExtractorName.CONFIG_KEYS: config_keys.extract,
    ExtractorName.ENTRYPOINTS: entrypoints.extract,
    ExtractorName.PORTS: ports.extract,
}


def extractors_for(health_source: health.HealthSource) -> dict[ExtractorName, Extractor]:
    """Every extractor of one run: ``REGISTRY`` plus ``service_health`` bound to ``health_source``."""
    return {**REGISTRY, ExtractorName.SERVICE_HEALTH: health.extractor(health_source)}


__all__ = ["REGISTRY", "Extractor", "extractors_for"]
