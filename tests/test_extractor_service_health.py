"""service_health: Magickit's MCP tool answer, as one fact per service."""

import asyncio
import json
import shutil
import time
from pathlib import Path

import pytest
from mcp.server import MCPServer

from verimend.collector import collect
from verimend.collector.extractors import REGISTRY, extractors_for
from verimend.collector.health import HealthSourceError, facts_from_health, magickit_health_source
from verimend.db import connection, migrate
from verimend.targets import ExtractorName, parse_targets

# The shape of spirrow-magickit's ``service_health`` tool (mcp/tools/health.py
# at 9d494e1): five services; healthy MCP-backed ones carry available_tools.
ANSWER = {
    "timestamp": "2026-10-02T07:00:00.000000",
    "status": "degraded",
    "services": {
        "cognilens": {
            "status": "healthy",
            "response_time_ms": 12.5,
            "url": "http://h:8003/sse",
            "available_tools": ["summarize", "analyze"],
        },
        "prismind": {"status": "error", "response_time_ms": 3.1, "url": "http://h:8002/sse", "error": "refused"},
        "lexora": {"status": "healthy", "response_time_ms": 4.0, "url": "http://h:8001"},
        "conclair": {"status": "healthy", "response_time_ms": 2.2, "url": "http://h:8005"},
        "github_mcp": {"status": "disabled", "url": "http://h:8116/mcp", "reason": "no GITHUB_MCP_PAT configured"},
    },
}


def _volatile_variant(answer: dict) -> dict:
    """The same state, with every per-call value changed."""
    other = json.loads(json.dumps(answer))
    other["timestamp"] = "2026-10-03T01:02:03.000000"
    other["status"] = "unhealthy"
    for entry in other["services"].values():
        if "response_time_ms" in entry:
            entry["response_time_ms"] += 100
        if "error" in entry:
            entry["error"] = "a differently worded error"
    return other


def _hashes(answer: dict) -> dict[str, str]:
    return {f.source_ref("", ""): f.content_hash() for f in facts_from_health(answer)}


# --- facts_from_health -------------------------------------------------------


def test_one_fact_per_service_with_only_the_stable_fields() -> None:
    facts = facts_from_health(ANSWER)

    # source_ref ignores the target repo and commit it is handed.
    assert [f.source_ref("o/ignored", "deadbeef") for f in facts] == [
        "magickit:service_health/cognilens",
        "magickit:service_health/conclair",
        "magickit:service_health/github_mcp",
        "magickit:service_health/lexora",
        "magickit:service_health/prismind",
    ]
    assert {f.source_kind for f in facts} == {"service_health"}
    by_service = {f.content["service"]: f.content for f in facts}
    assert by_service["cognilens"] == {
        "extractor": "service_health",
        "service": "cognilens",
        "status": "healthy",
        "url": "http://h:8003/sse",
        "available_tools": ["analyze", "summarize"],  # sorted
    }
    # no available_tools reported -> no key at all
    assert by_service["lexora"] == {
        "extractor": "service_health",
        "service": "lexora",
        "status": "healthy",
        "url": "http://h:8001",
    }
    assert by_service["prismind"]["status"] == "error"
    assert "error" not in by_service["prismind"]


def test_per_call_values_do_not_change_the_hash() -> None:
    assert _hashes(ANSWER) == _hashes(_volatile_variant(ANSWER))


def test_a_status_change_does_change_the_hash() -> None:
    changed = json.loads(json.dumps(ANSWER))
    changed["services"]["lexora"]["status"] = "unhealthy"
    before, after = _hashes(ANSWER), _hashes(changed)

    lexora = "magickit:service_health/lexora"
    assert before.pop(lexora) != after.pop(lexora)
    assert before == after


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"status": "healthy", "services": {}}, id="empty-services"),
        pytest.param({"status": "healthy"}, id="no-services"),
        pytest.param({"services": ["lexora"]}, id="services-not-an-object"),
        pytest.param({"services": {"lexora": {"url": "u"}}}, id="service-without-status"),
        pytest.param({"services": {"lexora": {"status": "healthy", "available_tools": "x"}}}, id="tools-not-a-list"),
        pytest.param({"services": {"lexora": {"status": "healthy", "url": 1}}}, id="url-not-a-string"),
        pytest.param([], id="not-an-object"),
    ],
)
def test_unusable_answers_are_errors(answer) -> None:
    with pytest.raises(HealthSourceError):
        facts_from_health(answer)


# --- magickit_health_source: the real MCP client against an in-process server --


def _server(answer) -> MCPServer:
    server = MCPServer("fake-magickit")

    @server.tool()
    async def service_health() -> dict:
        return answer

    return server


def test_mcp_source_returns_the_tool_answer() -> None:
    assert magickit_health_source(_server(ANSWER), timeout=10)() == ANSWER


def test_mcp_source_without_the_tool_is_an_error() -> None:
    with pytest.raises(HealthSourceError, match="returned an error"):
        magickit_health_source(MCPServer("no-tools"), timeout=10)()


@pytest.mark.parametrize(
    ("text", "error"),
    [("not json", "something other than JSON"), ("[1, 2]", "answered with list, not an object")],
)
def test_mcp_source_rejects_an_unusable_text_answer(text, error) -> None:
    # structured_output=False: no structured_content, so _call has to parse the text block.
    server = MCPServer("text-magickit")

    @server.tool(structured_output=False)
    async def service_health() -> str:
        return text

    with pytest.raises(HealthSourceError, match=error):
        magickit_health_source(server, timeout=10)()


def test_mcp_source_without_a_url_is_an_error() -> None:
    with pytest.raises(HealthSourceError, match="VERIMEND_MAGICKIT_URL is not set"):
        magickit_health_source(None, timeout=10)()


def test_mcp_source_times_out() -> None:
    server = MCPServer("slow")

    @server.tool()
    async def service_health() -> dict:
        await asyncio.sleep(30)
        return ANSWER

    # Either bound may fire first -- the client's read timeout or the outer
    # wait_for -- and they word it differently. What matters is that the call
    # ends as a HealthSourceError instead of hanging an unattended run.
    started = time.monotonic()
    with pytest.raises(HealthSourceError):
        magickit_health_source(server, timeout=0.5)()
    assert time.monotonic() - started < 10


# --- inside collect() --------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "v.sqlite3"
    migrate(path)
    return path


@pytest.fixture
def checkout(tmp_path: Path, write_repo):
    root = tmp_path / "source"
    commit = write_repo(root, {"README.md": "demo\n"})

    def copy(repo: str, dest: Path) -> str:
        shutil.copytree(root, dest)
        return commit

    return copy


def _targets(extractors: dict):
    return parse_targets(
        {"version": 1, "targets": [{"repo": "o/demo", "doc_globs": ["README.md"], "extractors": extractors}]}
    ).targets


class CountingSource:
    """A fake ``HealthSource``: the gate never reaches a real Magickit."""

    def __init__(self, answer=ANSWER, error: Exception | None = None) -> None:
        self.answer, self.error, self.calls = answer, error, 0

    def __call__(self) -> dict:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.answer


def test_collect_stores_one_fact_per_service(db, checkout) -> None:
    source = CountingSource()
    with connection(db) as conn:
        result = collect(conn, _targets({"service_health": True}), checkout, source)
        rows = conn.execute("SELECT source_kind, source_ref, content FROM fact ORDER BY id").fetchall()

    assert result.status == "succeeded"
    assert source.calls == 1
    assert result.stats["targets"]["o/demo"]["extractors"] == {"service_health": {"facts": 5}}
    assert set(result.stats) == {"targets"}  # recorded under the target, nothing at run level
    assert [r["source_ref"] for r in rows] == [f"magickit:service_health/{s}" for s in sorted(ANSWER["services"])]
    assert {r["source_kind"] for r in rows} == {"service_health"}
    assert all("timestamp" not in r["content"] and "response_time_ms" not in r["content"] for r in rows)


@pytest.mark.parametrize(
    "make_source",
    [
        pytest.param(lambda: CountingSource(error=HealthSourceError("ConnectError: refused")), id="unreachable"),
        pytest.param(lambda: CountingSource(error=HealthSourceError("no answer within 60s")), id="timeout"),
        pytest.param(lambda: CountingSource(answer={"status": "healthy", "services": {}}), id="empty-services"),
        pytest.param(lambda: CountingSource(answer={"oops": True}), id="malformed"),
    ],
)
def test_collect_contains_a_failed_source(db, checkout, make_source) -> None:
    with connection(db) as conn:
        result = collect(conn, _targets({"ports": True, "service_health": True}), checkout, make_source())
        health_rows = conn.execute("SELECT count(*) FROM fact WHERE source_kind = 'service_health'").fetchone()[0]
        run = conn.execute("SELECT status FROM crawl_run WHERE id = ?", (result.run_id,)).fetchone()

    extractors = result.stats["targets"]["o/demo"]["extractors"]
    assert result.status == "partial" == run["status"]
    assert "error" in extractors["service_health"]
    assert extractors["ports"] == {"facts": 0}  # the other extractors of the target still ran
    assert health_rows == 0


def test_failed_checkout_never_calls_the_source(db) -> None:
    def broken(repo: str, dest: Path) -> str:
        raise OSError("disk full")

    source = CountingSource()
    with connection(db) as conn:
        result = collect(conn, _targets({"service_health": True}), broken, source)

    assert source.calls == 0
    assert result.status == "partial"
    assert result.stats["targets"]["o/demo"] == {"error": "checkout failed: OSError: disk full"}


# --- coverage ----------------------------------------------------------------


def test_every_extractor_name_resolves_to_an_extractor() -> None:
    """No ExtractorName may fall through to a "not implemented yet" path."""
    resolved = extractors_for(CountingSource())
    assert set(resolved) == set(ExtractorName)
    assert all(callable(e) for e in resolved.values())
    # REGISTRY is the tree-reading part; service_health is the only name bound per run.
    assert set(ExtractorName) - set(REGISTRY) == {ExtractorName.SERVICE_HEALTH}


def test_a_run_with_every_extractor_reports_no_skip(db, checkout) -> None:
    every = {name.value: True for name in ExtractorName}
    with connection(db) as conn:
        result = collect(conn, _targets(every), checkout, CountingSource())

    extractors = result.stats["targets"]["o/demo"]["extractors"]
    assert set(extractors) == {name.value for name in ExtractorName}
    assert all(set(outcome) == {"facts"} for outcome in extractors.values())
