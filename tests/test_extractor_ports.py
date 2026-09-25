"""ports: port numbers where their context says they are ports."""

from verimend.collector.extractors import ports

PY = '''
"""Talks to http://localhost:7300 -- a docstring, not configuration."""
import os

port: int = 8004
MCP_PORT = int(os.environ.get("X_MCP_PORT", "8114"))
LEXORA_URL = "http://localhost:8001/v1"
timeout = 8000
report_interval = 3600
transport = 9999


def serve(host="0.0.0.0", listen_port=8118):
    uvicorn.run(app, port=8117)
    cfg.get("port", 8113)
    getattr(settings, "mcp_port", 8116)
    run(port=80)

BANNER = """
first line
see http://127.0.0.1:8120/health
"""
'''

TEXT = """
# PORT=1111 is a comment
MAGICKIT_PORT=8113
url: "http://localhost:8110"
exec uvicorn app --host 127.0.0.1 --port 8113
bind 0.0.0.0:8443
report: 5000
--transport 6000
"""


def _hits(tree):
    return sorted((f.path, f.line, f.content["port"], f.content["via"]) for f in ports.extract(tree))


def test_python_contexts(make_tree) -> None:
    hits = [(line, port, via) for _, line, port, via in _hits(make_tree({"app.py": PY}))]

    assert hits == [
        (4, 8004, "assign"),
        (5, 8114, "assign"),
        (6, 8001, "url"),
        (12, 8118, "default"),
        (13, 8117, "keyword"),
        (14, 8113, "lookup"),
        (15, 8116, "lookup"),
        (20, 8120, "url"),  # line of the match inside the multi-line string
    ]


def test_text_file_contexts(make_tree) -> None:
    hits = [(line, port, via) for _, line, port, via in _hits(make_tree({".env.example": TEXT}))]

    assert hits == [
        (2, 8113, "key_value"),
        (3, 8110, "url"),
        (4, 8113, "flag"),
        (5, 8443, "host_port"),
    ]


def test_context_is_the_source_line(make_tree) -> None:
    facts = list(ports.extract(make_tree({"start.sh": "exec uvicorn app --port 8113\n"})))

    assert [f.content for f in facts] == [
        {"extractor": "ports", "port": 8113, "via": "flag", "context": "exec uvicorn app --port 8113"}
    ]
    assert facts[0].source_kind == "file"


def test_markdown_and_unknown_files_are_not_read(make_tree) -> None:
    tree = make_tree({"README.md": "Runs on http://localhost:8004\n", "notes.txt": "port: 8004\n"})
    assert list(ports.extract(tree)) == []
