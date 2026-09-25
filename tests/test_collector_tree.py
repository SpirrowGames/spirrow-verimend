"""RepoTree decides which files count as reality for every extractor."""

from verimend.collector import Fact


def test_only_tracked_non_test_files(make_tree, tmp_path) -> None:
    tree = make_tree(
        {
            "src/app.py": "x = 1\n",
            "tests/test_app.py": "x = 2\n",
            "src/pkg/fixtures/data.py": "x = 3\n",
            "tests.py": "x = 4\n",  # a file named like the dir is still product code
        }
    )
    (tree.root / "untracked.py").write_text("x = 5\n")

    assert tree.files == ("src/app.py", "tests.py")


def test_unparsable_python_is_skipped_and_recorded(make_tree) -> None:
    tree = make_tree({"bad.py": "def (:\n", "good.py": "x = 1\n"})

    assert tree.parse("bad.py") is None
    assert tree.parse("good.py") is not None
    assert tree.skipped["bad.py"].startswith("unparsable: SyntaxError")


def test_non_utf8_is_skipped_and_recorded(make_tree) -> None:
    tree = make_tree({"latin1.py": "x = 1\n"})
    (tree.root / "latin1.py").write_bytes(b"s = '\xe9'\n")

    assert tree.text("latin1.py") is None
    assert tree.skipped["latin1.py"] == "not UTF-8"


def test_fact_hash_ignores_location() -> None:
    a = Fact("file", "a.py", 1, {"port": 8118, "extractor": "ports"})
    b = Fact("file", "b.py", 99, {"extractor": "ports", "port": 8118})

    assert a.content_hash() == b.content_hash()
    assert a.source_ref("o/r", "abc") == "o/r@abc:a.py:1"
    assert Fact("config", "pyproject.toml", None, {}).source_ref("o/r", "abc") == "o/r@abc:pyproject.toml"
