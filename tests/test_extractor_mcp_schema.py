"""mcp_schema: FastMCP tool definitions from the AST."""

from verimend.collector.extractors import mcp_schema

TOOLS = '''
from fastmcp import Context, FastMCP

mcp = FastMCP("x")
CATALOG = "..."


def register(mcp):
    @mcp.tool()
    async def research(query: str, max_tokens: int = 1000, *, tags: list[str] | None = None, ctx: Context | None = None) -> dict:
        """Search and compress.

        Longer text that is not the summary.
        """


@mcp.tool
def bare(x):
    pass


@server.tool(name="github", description="Call a GitHub operation. " + CATALOG)
async def dispatch(operation: str, arguments: dict | None = None):
    """Not the description: the argument wins."""


@mcp.tool(name=TOOL_NAME)
def computed():
    pass


def spec_status(execution_id: str, user: str = "") -> dict:
    """Status of an execution."""


mcp.tool()(spec_status)
mcp.tool()(defined_elsewhere)


@mcp.resource("x://y")
def not_a_tool():
    pass
'''


def _by_function(tree):
    return {f.content["function"]: f for f in mcp_schema.extract(tree)}


def test_extracts_every_registration_form(make_tree) -> None:
    facts = _by_function(make_tree({"src/tools.py": TOOLS}))

    assert set(facts) == {"research", "bare", "dispatch", "computed", "spec_status", "defined_elsewhere"}
    assert all(f.source_kind == "tool_schema" and f.path == "src/tools.py" for f in facts.values())


def test_signature_and_docstring_summary(make_tree) -> None:
    research = _by_function(make_tree({"t.py": TOOLS}))["research"].content

    assert research["tool"] == "research"
    assert research["summary"] == "Search and compress."
    assert research["description_source"] == "docstring"
    assert research["returns"] == "dict"
    # ctx is injected by FastMCP and hidden from the schema
    assert research["parameters"] == [
        {"name": "query", "annotation": "str", "default": None, "required": True},
        {"name": "max_tokens", "annotation": "int", "default": "1000", "required": False},
        {"name": "tags", "annotation": "list[str] | None", "default": "None", "required": False},
    ]


def test_explicit_name_and_description_argument(make_tree) -> None:
    facts = _by_function(make_tree({"t.py": TOOLS}))

    dispatch = facts["dispatch"].content
    assert dispatch["tool"] == "github"
    assert dispatch["description_source"] == "argument"
    assert dispatch["summary"] == "Call a GitHub operation."

    computed = facts["computed"].content
    assert computed["tool"] is None
    assert computed["tool_expr"] == "TOOL_NAME"


def test_call_form_resolves_the_function(make_tree) -> None:
    facts = _by_function(make_tree({"t.py": TOOLS}))

    status = facts["spec_status"]
    assert status.content["summary"] == "Status of an execution."
    assert [p["name"] for p in status.content["parameters"]] == ["execution_id", "user"]
    assert status.line == TOOLS.lstrip("\n").splitlines().index("mcp.tool()(spec_status)") + 1

    # The registration is a fact even when the function cannot be resolved.
    unknown = facts["defined_elsewhere"].content
    assert unknown["tool"] == "defined_elsewhere"
    assert unknown["parameters"] is None


def test_tools_in_tests_are_not_facts(make_tree) -> None:
    tree = make_tree({"tests/test_tools.py": TOOLS})
    assert list(mcp_schema.extract(tree)) == []


OPEN = '''
@mcp.tool()
def closed(a: int, *, b: str = "x"):
    pass


@mcp.tool()
def with_kwargs(a: int, **kwargs):
    pass


@mcp.tool()
def with_args(*args):
    pass


@mcp.tool()
def with_both(a, *rest, **options):
    pass


mcp.tool()(unresolved)
'''


def test_open_parameters_records_star_args_and_kwargs(make_tree) -> None:
    """``*args`` / ``**kwargs`` are not listed as parameters, but their existence is a fact.

    Without it, a closed signature and one accepting arbitrary names look the
    same, and a reconciler would call a documented ``**kwargs`` option stale.
    """
    facts = _by_function(make_tree({"t.py": OPEN}))

    assert facts["closed"].content["open_parameters"] is False
    assert facts["with_kwargs"].content["open_parameters"] is True
    assert facts["with_args"].content["open_parameters"] is True
    assert facts["with_both"].content["open_parameters"] is True
    # The star parameters themselves stay out of the named list.
    assert [p["name"] for p in facts["with_both"].content["parameters"]] == ["a"]
    assert facts["with_args"].content["parameters"] == []


def test_open_parameters_is_unknown_when_the_function_is_unresolved(make_tree) -> None:
    unresolved = _by_function(make_tree({"t.py": OPEN}))["unresolved"].content
    assert unresolved["parameters"] is None
    assert unresolved["open_parameters"] is None
