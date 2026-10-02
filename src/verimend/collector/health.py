"""``service_health``: the live state of the Spirrow services, as facts.

Unlike the other extractors this one does not read the checked-out tree:
running state is not a property of a commit. It asks Magickit's MCP tool
``service_health`` (docs/design.md section 5.1) and turns the answer into one
fact per service.

The REST ``GET /health`` of Magickit is deliberately *not* the source: it
covers only three of the five services and reports a hard-coded version, so
reading it would store a partial picture plus a constant as "facts".

What goes into ``content`` and what stays out
---------------------------------------------
Only what describes the state: ``extractor``, ``service``, ``status``,
``url`` and ``available_tools`` (sorted; the key is omitted when the service
did not report it). The per-call values -- ``timestamp``,
``response_time_ms``, the wording of ``error``, and the aggregated top-level
``status`` -- change on every call, so storing them would give the same state
a new ``content_hash`` on every run. When the observation was made is
``crawl_run.started_at``.

Where the facts point
---------------------
``source_ref`` is ``magickit:service_health/<service>``. It carries no commit
and no target repository, because the state belongs to neither.

Failure
-------
Unreachable, timed out, an MCP error, a malformed answer, or an answer with no
services at all all raise. The collector contains that per extractor (see
``verimend.collector.run``): the error is recorded under the target, nothing
is stored, and the run is ``partial``. An empty ``services`` is an error and
not "zero facts", because storing zero facts reads as "every service is gone".
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from typing import Any

from verimend.collector.facts import LiveFact
from verimend.collector.tree import RepoTree

EXTRACTOR = "service_health"
TOOL_NAME = "service_health"

# () -> the decoded answer of Magickit's ``service_health`` MCP tool.
HealthSource = Callable[[], dict[str, Any]]


class HealthSourceError(RuntimeError):
    """The health answer could not be obtained, or is not usable as facts."""


def magickit_health_source(server: Any, timeout: float) -> HealthSource:
    """A ``HealthSource`` calling the ``service_health`` tool over MCP.

    ``server`` is the Streamable HTTP endpoint (``VERIMEND_MAGICKIT_URL``,
    e.g. ``http://host:8114/mcp``). ``None`` -- the setting is absent --
    yields a source that fails loudly instead of guessing an address. Tests
    pass an in-process ``mcp.server.MCPServer`` instead of a URL, which drives
    the same client code without a network.

    ``timeout`` bounds the whole exchange (connect, initialize, call), so an
    endpoint that accepts the connection and never answers cannot hang an
    unattended run.
    """

    def source() -> dict[str, Any]:
        if server is None:
            raise HealthSourceError("VERIMEND_MAGICKIT_URL is not set")
        try:
            return asyncio.run(asyncio.wait_for(_call(server, timeout), timeout))
        except HealthSourceError:
            raise
        except TimeoutError as exc:
            raise HealthSourceError(f"no answer from {_name(server)} within {timeout:g}s") from exc
        except Exception as exc:  # noqa: BLE001 - normalised into one error the collector records
            raise HealthSourceError(f"{_name(server)}: {_describe(exc)}") from exc

    return source


async def _call(server: Any, timeout: float) -> dict[str, Any]:
    from mcp import Client  # imported lazily: only a run with service_health enabled needs it

    async with Client(server, read_timeout_seconds=timeout) as client:
        result = await client.call_tool(TOOL_NAME, {})
    if result.is_error:
        raise HealthSourceError(f"tool {TOOL_NAME} returned an error: {_text(result.content)}")
    if isinstance(result.structured_content, dict):
        return result.structured_content
    try:
        payload = json.loads(_text(result.content))
    except ValueError as exc:
        raise HealthSourceError(f"tool {TOOL_NAME} answered with something other than JSON") from exc
    if not isinstance(payload, dict):
        raise HealthSourceError(f"tool {TOOL_NAME} answered with {type(payload).__name__}, not an object")
    return payload


def facts_from_health(answer: Any) -> list[LiveFact]:
    """One ``service_health`` fact per service, sorted by service name."""
    if not isinstance(answer, dict) or not isinstance(answer.get("services"), dict):
        raise HealthSourceError("malformed answer: no 'services' object")
    services = answer["services"]
    if not services:
        raise HealthSourceError("answer lists no services")

    facts: list[LiveFact] = []
    for name in sorted(services):
        entry = services[name]
        if not isinstance(entry, dict) or not isinstance(entry.get("status"), str):
            raise HealthSourceError(f"malformed answer: service {name!r} has no status")
        content: dict[str, Any] = {"extractor": EXTRACTOR, "service": name, "status": entry["status"]}
        if "url" in entry:
            if not isinstance(entry["url"], str):
                raise HealthSourceError(f"malformed answer: service {name!r} has a non-string url")
            content["url"] = entry["url"]
        if "available_tools" in entry:
            tools = entry["available_tools"]
            if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
                raise HealthSourceError(f"malformed answer: service {name!r} available_tools is not a list of names")
            content["available_tools"] = sorted(tools)
        facts.append(LiveFact(source_kind="service_health", ref=f"magickit:service_health/{name}", content=content))
    return facts


def extractor(source: HealthSource) -> Callable[[RepoTree], Iterable[LiveFact]]:
    """Adapt ``source`` to the extractor shape so it runs in the ordinary target loop.

    The tree is ignored: see the module docstring.
    """

    def extract(tree: RepoTree) -> Iterable[LiveFact]:
        return facts_from_health(source())

    return extract


def _text(content: list[Any]) -> str:
    return "".join(getattr(block, "text", "") for block in content)


def _name(server: Any) -> str:
    return server if isinstance(server, str) else f"<{type(server).__name__}>"


def _describe(exc: BaseException) -> str:
    """The first leaf of an exception group: anyio wraps connect errors in one."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{exc.__class__.__name__}: {exc}"
