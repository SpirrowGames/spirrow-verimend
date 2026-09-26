"""entrypoints: console scripts, systemd ExecStart, runnable modules."""

from verimend.collector.extractors import entrypoints

PYPROJECT = """
[project]
name = "demo"

[project.scripts]
demo = "demo.main:main"
"demo-mcp" = "demo.mcp_server:main"

[project.gui-scripts]
demo-gui = "demo.gui:run"
"""

UNIT = """
[Unit]
Description=Demo
ExecStart=/not/in/service/section

[Service]
Type=simple
ExecStart=/opt/demo/venv/bin/python \\
    -m demo.mcp_server --port 8114
Restart=always
"""

MAIN_GUARD = """
def main():
    pass


if __name__ == "__main__":
    main()
"""


def _by_kind(tree):
    out = {}
    for fact in entrypoints.extract(tree):
        out.setdefault(fact.content["kind"], []).append(fact)
    return out


def test_pyproject_scripts(make_tree) -> None:
    facts = _by_kind(make_tree({"pyproject.toml": PYPROJECT}))

    scripts = {f.content["name"]: (f.content["target"], f.line) for f in facts["console_script"]}
    assert scripts == {"demo": ("demo.main:main", 5), "demo-mcp": ("demo.mcp_server:main", 6)}
    assert [f.content["name"] for f in facts["gui_script"]] == ["demo-gui"]


def test_systemd_exec_start_joins_continuations(make_tree) -> None:
    facts = _by_kind(make_tree({"deploy/demo.service": UNIT}))["systemd_exec"]

    assert len(facts) == 1
    assert facts[0].content["command"] == "/opt/demo/venv/bin/python -m demo.mcp_server --port 8114"
    assert facts[0].content["unit"] == "demo.service"
    assert facts[0].line == 7


def test_runnable_modules(make_tree) -> None:
    tree = make_tree(
        {
            "src/demo/__init__.py": "",
            "src/demo/server.py": MAIN_GUARD,
            "src/demo/lib.py": "x = 1\n",
            "src/demo/cli/__init__.py": "",
            "src/demo/cli/__main__.py": "print('hi')\n",
            "scripts/tool.py": MAIN_GUARD,  # scripts/ is not a package
            "tests/test_x.py": MAIN_GUARD,
        }
    )
    commands = {f.path: f.content["command"] for f in _by_kind(tree)["python_module"]}

    assert commands == {
        "src/demo/server.py": "python -m demo.server",
        "src/demo/cli/__main__.py": "python -m demo.cli",
        "scripts/tool.py": "python scripts/tool.py",
    }


def test_nested_main_guard_is_not_an_entry_point(make_tree) -> None:
    tree = make_tree({"m.py": "def f():\n    if __name__ == '__main__':\n        pass\n"})
    assert list(entrypoints.extract(tree)) == []
