"""M3b: drafts to branches and PRs (verimend/mender/publish.py, design 5.4).

The remotes are local bare repositories and ``gh`` is a fake: nothing here reaches GitHub.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from verimend import __main__ as cli
from verimend.collector.facts import canonical_json
from verimend.db import connection, migrate
from verimend.mender import PrItem, PublishConfig, build_patch, build_pr_body, mend_stage_failed, publish_prs
from verimend.mender.draft import _record as record_mend
from verimend.mender.draft import MendPass, MenderSession, check_replacement
from verimend.mender.publish import Evidence
from verimend.pipeline import run_pipeline
from verimend.settings import Settings
from verimend.targets import parse_targets

A, B = "o/alpha", "o/beta"
DOC = "".join(f"line {n}\n" for n in range(1, 21))
STARTED = "2026-10-28T23:30:00+00:00"


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false",
         "-C", str(root), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


class Remotes:
    """Bare repositories under ``root`` laid out as ``<root>/<owner>/<name>.git``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.commits: dict[str, str] = {}

    @property
    def url(self) -> str:
        return self.root.as_uri()

    def bare(self, repo: str) -> Path:
        return self.root / f"{repo}.git"

    def create(self, repo: str, text: str = DOC) -> str:
        work = self.root / "work" / repo
        work.mkdir(parents=True)
        git(work, "init", "-q", "-b", "trunk")
        (work / "README.md").write_bytes(text.encode("utf-8"))
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "base")
        bare = self.bare(repo)
        bare.parent.mkdir(parents=True, exist_ok=True)
        git(self.root, "clone", "-q", "--bare", str(work), str(bare))
        self.commits[repo] = git(work, "rev-parse", "HEAD")
        return self.commits[repo]

    def advance(self, repo: str) -> None:
        """A new commit on the default branch, moving every line down."""
        work = self.root / "work" / repo
        (work / "README.md").write_bytes(("new first line\n" + DOC).encode("utf-8"))
        git(work, "commit", "-q", "-am", "moved")
        git(work, "push", "-q", str(self.bare(repo)), "HEAD:trunk")

    def reject_pushes(self, repo: str) -> None:
        hook = self.bare(repo) / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho pushed >> attempts\nexit 1\n", newline="\n")
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

    def accept_pushes(self, repo: str) -> None:
        (self.bare(repo) / "hooks" / "pre-receive").unlink()

    def push_attempts(self, repo: str) -> int:
        path = self.bare(repo) / "attempts"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def branches(self, repo: str) -> list[str]:
        out = git(self.bare(repo), "for-each-ref", "--format=%(refname:short)", "refs/heads/")
        return sorted(b for b in out.splitlines() if b != "trunk")

    def show(self, repo: str, branch: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.bare(repo)), "show", f"{branch}:README.md"],
            check=True, capture_output=True,
        ).stdout.decode("utf-8")


class FakeGh:
    """An executable recording each ``gh`` call; it fails while ``<dir>/fail`` holds a count above 0."""

    def __init__(self, root: Path) -> None:
        root.mkdir()
        self.root = root
        self.log = root / "calls.jsonl"
        script = root / "fake_gh.py"
        script.write_text(
            "import json, pathlib, sys\n"
            f"root = pathlib.Path({str(root)!r})\n"
            "body = sys.stdin.buffer.read().decode('utf-8')\n"
            "with (root / 'calls.jsonl').open('a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps({'args': sys.argv[1:], 'body': body}) + '\\n')\n"
            "fail = root / 'fail'\n"
            "left = int(fail.read_text()) if fail.exists() else 0\n"
            "if left > 0:\n"
            "    fail.write_text(str(left - 1))\n"
            "    print('gh: server said no', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "repo = sys.argv[sys.argv.index('--repo') + 1]\n"
            "print('Creating pull request')\n"
            "print(f'https://github.example/{repo}/pull/1')\n",
            encoding="utf-8",
        )
        if os.name == "nt":
            self.bin = root / "gh.cmd"
            self.bin.write_text(f'@"{sys.executable}" "{script}" %*\n')
        else:
            self.bin = root / "gh"
            self.bin.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
            self.bin.chmod(0o755)

    def fail_next(self, count: int) -> None:
        (self.root / "fail").write_text(str(count))

    @property
    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "v.sqlite3"
    migrate(db)
    with connection(db) as c:
        yield c


@pytest.fixture
def remotes(tmp_path: Path) -> Remotes:
    root = tmp_path / "remotes"
    root.mkdir()
    return Remotes(root)


@pytest.fixture
def gh(tmp_path: Path) -> FakeGh:
    return FakeGh(tmp_path / "gh")


def config(remotes: Remotes, gh: FakeGh, tmp_path: Path, mode: str = "github", **kwargs) -> PublishConfig:
    return PublishConfig(
        mode=mode, author_name="Verimend Bot", author_email="bot@example.invalid", gh_bin=str(gh.bin),
        github_base_url=remotes.url, timeout_s=60.0, artefact_dir=tmp_path / "artefacts", **kwargs,
    )


def make_run(conn: sqlite3.Connection, targets: dict) -> int:
    stats = {"targets": targets, "mend": {"drafted": 0}}
    run = conn.execute(
        "INSERT INTO crawl_run (started_at, status, stats_json) VALUES (?, 'running', ?)",
        (STARTED, canonical_json(stats)),
    ).lastrowid
    conn.commit()
    return run


def add_draft(
    conn: sqlite3.Connection, run: int, repo: str, anchor: tuple[int, int] = (6, 6), *, method: str = "llm",
    new: str = "fixed", text: str = DOC, patch: str | None = None, claim: str = "the claim", path: str = "README.md",
) -> int:
    fact = conn.execute(
        "INSERT INTO fact (run_id, source_kind, source_ref, content, content_hash) VALUES (?, 'config', ?, ?, 'h')",
        (run, f"{repo}@abc:src/config.py:4", canonical_json({"port": 8120})),
    ).lastrowid
    claim_id = conn.execute(
        "INSERT INTO claim (run_id, doc_repo, doc_path, anchor_start, anchor_end, text, claim_type)"
        " VALUES (?, ?, ?, ?, ?, ?, 'port')",
        (run, repo, path, *anchor, claim),
    ).lastrowid
    if patch is None:
        patch = build_patch(path, text.splitlines(keepends=True), anchor, new.splitlines())
    verdict = conn.execute(
        "INSERT INTO verdict (claim_id, result, method, evidence_json, patch_draft) VALUES (?, 'stale', ?, ?, ?)",
        (claim_id, method, json.dumps({"fact_ids": [fact]}), patch),
    ).lastrowid
    conn.commit()
    return verdict


def stored(conn: sqlite3.Connection, run: int) -> dict:
    return json.loads(conn.execute("SELECT stats_json FROM crawl_run WHERE id = ?", (run,)).fetchone()[0])


# ------------------------------------------------------------- publishing


def test_github_mode_pushes_one_commit_from_the_runs_commit_and_opens_a_pr(conn, remotes, gh, tmp_path) -> None:
    base = remotes.create(A)
    remotes.advance(A)  # the default branch moved on after the crawl
    run = make_run(conn, {A: {"commit": base}})
    verdict = add_draft(conn, run, A, new="fixed")
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A] == {
        "mode": "github", "branch": "verimend/2026-10-28", "base_commit": base,
        "url": f"https://github.example/{A}/pull/1", "applied": [verdict], "skipped": [], "pushed": True,
    }
    assert stored(conn, run)["mend"]["prs"] == prs and stored(conn, run)["mend"]["drafted"] == 0
    bare = remotes.bare(A)
    assert git(bare, "rev-parse", "verimend/2026-10-28^") == base  # one commit, on the run's commit
    assert git(bare, "log", "-1", "--format=%an <%ae>", "verimend/2026-10-28") == "Verimend Bot <bot@example.invalid>"
    assert remotes.show(A, "verimend/2026-10-28").splitlines()[5] == "fixed"
    args = gh.calls[0]["args"]
    assert args[args.index("--base") + 1] == "trunk" and args[args.index("--head") + 1] == "verimend/2026-10-28"
    assert "the claim" in gh.calls[0]["body"]


def test_dry_run_writes_the_body_and_never_touches_the_remote(conn, remotes, gh, tmp_path) -> None:
    base = remotes.create(A)
    run = make_run(conn, {A: {"commit": base}})
    verdict = add_draft(conn, run, A)
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path, mode="dry_run"))
    record = prs[A]
    assert record["mode"] == "dry_run" and record["url"] is None and record["applied"] == [verdict]
    assert record["branch"] == "verimend/2026-10-28" and record["base_commit"] == base and "pushed" not in record
    assert Path(record["body_path"]).read_text(encoding="utf-8").count("the claim") == 1
    assert remotes.branches(A) == [] and gh.calls == []


def test_a_configured_pr_base_is_used(conn, remotes, gh, tmp_path) -> None:
    base = remotes.create(A)
    run = make_run(conn, {A: {"commit": base}})
    add_draft(conn, run, A)
    publish_prs(conn, run, config(remotes, gh, tmp_path, pr_bases={A: "develop"}))
    args = gh.calls[0]["args"]
    assert args[args.index("--base") + 1] == "develop"


def test_no_drafts_makes_no_branch_and_no_failure(conn, remotes, gh, tmp_path) -> None:
    run = make_run(conn, {A: {"commit": "abc"}})
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs == {A: {"mode": "github", "reason": "no_drafts"}}
    assert gh.calls == [] and not mend_stage_failed({"prs": prs})


def test_a_repository_without_a_recorded_commit_is_not_published(conn, remotes, gh, tmp_path) -> None:
    remotes.create(A)
    run = make_run(conn, {A: {"error": "checkout failed"}})
    add_draft(conn, run, A)
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs == {A: {"mode": "github", "reason": "no_base_commit"}}
    assert remotes.branches(A) == [] and gh.calls == [] and mend_stage_failed({"prs": prs})


def test_a_commit_the_remote_does_not_have_is_a_worktree_error(conn, remotes, gh, tmp_path) -> None:
    remotes.create(A)
    run = make_run(conn, {A: {"commit": "0" * 40}})
    add_draft(conn, run, A)
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A]["publish_error"]["stage"] == "worktree" and mend_stage_failed({"prs": prs})
    assert gh.calls == []


# ----------------------------------------------------------------- apply


def test_a_draft_that_does_not_apply_is_dropped_alone(conn, remotes, gh, tmp_path) -> None:
    base = remotes.create(A)
    run = make_run(conn, {A: {"commit": base}})
    good = add_draft(conn, run, A, (6, 6))
    other_text = DOC.replace("line 15\n", "something else\n")
    bad = add_draft(conn, run, A, (15, 15), text=other_text)
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A]["applied"] == [good]
    assert prs[A]["skipped"] == [{"verdict_id": bad, "reason": "apply_conflict"}]
    assert not mend_stage_failed({"prs": prs})


def test_overlapping_anchors_keep_one_draft(conn, remotes, gh, tmp_path) -> None:
    base = remotes.create(A)
    run = make_run(conn, {A: {"commit": base}})
    wide = add_draft(conn, run, A, (6, 8), new="wide")
    inner = add_draft(conn, run, A, (7, 7), new="inner")
    same = add_draft(conn, run, A, (7, 7), new="same anchor")
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A]["applied"] == [inner]  # order: start descending, then verdict id
    assert {s["verdict_id"] for s in prs[A]["skipped"]} == {wide, same}
    lines = remotes.show(A, "verimend/2026-10-28").splitlines()
    assert lines[5:8] == ["line 6", "inner", "line 8"]


def test_neighbouring_anchors_inside_each_others_context_both_apply(conn, remotes, gh, tmp_path) -> None:
    base = remotes.create(A)
    run = make_run(conn, {A: {"commit": base}})
    first = add_draft(conn, run, A, (6, 6), new="six")
    second = add_draft(conn, run, A, (7, 7), new="seven\nmore")
    third = add_draft(conn, run, A, (9, 9), new="nine")
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A]["applied"] == [third, second, first] and prs[A]["skipped"] == []
    lines = remotes.show(A, "verimend/2026-10-28").splitlines()
    assert lines[4:11] == ["line 5", "six", "seven", "more", "line 8", "nine", "line 10"]


def test_a_repeated_line_is_changed_where_the_draft_says(conn, remotes, gh, tmp_path) -> None:
    text = "".join(["same\n", *[f"x{n}\n" for n in range(10)], "same\n", *[f"y{n}\n" for n in range(10)], "same\n"])
    base = remotes.create(A, text)
    run = make_run(conn, {A: {"commit": base}})
    add_draft(conn, run, A, (12, 12), new="changed", text=text)
    publish_prs(conn, run, config(remotes, gh, tmp_path))
    lines = remotes.show(A, "verimend/2026-10-28").splitlines()
    assert [n for n, line in enumerate(lines, 1) if line == "changed"] == [12] and lines.count("same") == 2


def test_crlf_and_no_final_newline_documents_apply(conn, remotes, gh, tmp_path) -> None:
    text = "one\r\ntwo\r\nthree\r\nlast without newline"
    base = remotes.create(A, text)
    run = make_run(conn, {A: {"commit": base}})
    last = add_draft(conn, run, A, (4, 4), new="final", text=text)
    second = add_draft(conn, run, A, (2, 2), new="TWO", text=text)
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A]["applied"] == [last, second] and prs[A]["skipped"] == []


def test_nothing_applying_means_no_branch(conn, remotes, gh, tmp_path) -> None:
    base = remotes.create(A)
    run = make_run(conn, {A: {"commit": base}})
    bad = add_draft(conn, run, A, (15, 15), text=DOC.replace("line 15\n", "else\n"))
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A] == {
        "mode": "github", "reason": "all_skipped", "skipped": [{"verdict_id": bad, "reason": "apply_conflict"}],
    }
    assert remotes.branches(A) == [] and gh.calls == []


def test_the_worktree_is_removed_on_success_and_on_failure(conn, remotes, gh, tmp_path, monkeypatch) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    base = remotes.create(A)
    base_b = remotes.create(B)
    remotes.reject_pushes(B)
    run = make_run(conn, {A: {"commit": base}, B: {"commit": base_b}})
    add_draft(conn, run, A)
    add_draft(conn, run, B)
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A]["url"] and "publish_error" in prs[B]
    assert list(scratch.iterdir()) == []


# ---------------------------------------------------------------- branches


def test_a_taken_branch_name_gets_the_run_id_and_keeps_the_old_branch(conn, remotes, gh, tmp_path) -> None:
    base = remotes.create(A)
    work_tip = git(remotes.bare(A), "rev-parse", "trunk")
    git(remotes.bare(A), "branch", "verimend/2026-10-28", work_tip)
    run = make_run(conn, {A: {"commit": base}})
    add_draft(conn, run, A)
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A]["branch"] == f"verimend/2026-10-28-r{run}"
    assert remotes.branches(A) == ["verimend/2026-10-28", f"verimend/2026-10-28-r{run}"]
    assert git(remotes.bare(A), "rev-parse", "verimend/2026-10-28") == work_tip


def test_the_branch_date_is_the_runs_start_in_utc(conn, remotes, gh, tmp_path) -> None:
    base = remotes.create(A)
    run = conn.execute(
        "INSERT INTO crawl_run (started_at, status, stats_json) VALUES ('2026-10-29T01:00:00+09:00', 'running', ?)",
        (canonical_json({"targets": {A: {"commit": base}}}),),
    ).lastrowid
    add_draft(conn, run, A)
    assert publish_prs(conn, run, config(remotes, gh, tmp_path))[A]["branch"] == "verimend/2026-10-28"


# ---------------------------------------------------- several repositories


def test_two_repositories_each_get_their_own_record_and_base(conn, remotes, gh, tmp_path) -> None:
    base_a, base_b = remotes.create(A), remotes.create(B, DOC + "tail\n")
    assert base_a != base_b
    run = make_run(conn, {A: {"commit": base_a}, B: {"commit": base_b}})
    add_draft(conn, run, A)
    add_draft(conn, run, B, text=DOC + "tail\n")
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert set(prs) == {A, B}
    assert (prs[A]["base_commit"], prs[B]["base_commit"]) == (base_a, base_b)
    assert git(remotes.bare(A), "rev-parse", "verimend/2026-10-28^") == base_a
    assert git(remotes.bare(B), "rev-parse", "verimend/2026-10-28^") == base_b
    assert "pr_url" not in stored(conn, run)
    assert conn.execute("SELECT pr_url FROM crawl_run WHERE id = ?", (run,)).fetchone()[0] is None


def test_one_repository_without_drafts_beside_one_with(conn, remotes, gh, tmp_path) -> None:
    base_a, base_b = remotes.create(A), remotes.create(B)
    run = make_run(conn, {A: {"commit": base_a}, B: {"commit": base_b}})
    add_draft(conn, run, B)
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A] == {"mode": "github", "reason": "no_drafts"} and prs[B]["url"]
    assert remotes.branches(A) == []


def test_a_failed_push_keeps_the_others_record_and_publishing_again_resumes_only_it(
    conn, remotes, gh, tmp_path
) -> None:
    base_a, base_b = remotes.create(A), remotes.create(B)
    remotes.reject_pushes(B)
    run = make_run(conn, {A: {"commit": base_a}, B: {"commit": base_b}})
    add_draft(conn, run, A)
    add_draft(conn, run, B)
    cfg = config(remotes, gh, tmp_path)

    prs = publish_prs(conn, run, cfg)
    assert prs[A]["url"] == f"https://github.example/{A}/pull/1"
    assert prs[B]["publish_error"]["stage"] == "push" and prs[B]["url"] is None
    assert remotes.push_attempts(B) == 2  # retried exactly once
    assert mend_stage_failed({"prs": prs})
    tip_a = git(remotes.bare(A), "rev-parse", "verimend/2026-10-28")

    remotes.accept_pushes(B)
    prs = publish_prs(conn, run, cfg)
    assert prs[A]["url"] and prs[B]["url"] == f"https://github.example/{B}/pull/1"
    assert "publish_error" not in prs[B] and not mend_stage_failed({"prs": prs})
    assert git(remotes.bare(A), "rev-parse", "verimend/2026-10-28") == tip_a  # A was not pushed again
    assert remotes.branches(A) == ["verimend/2026-10-28"]
    assert [c["args"][c["args"].index("--repo") + 1] for c in gh.calls] == [A, B]


def test_publishing_a_finished_run_again_does_nothing(conn, remotes, gh, tmp_path) -> None:
    base = remotes.create(A)
    run = make_run(conn, {A: {"commit": base}})
    add_draft(conn, run, A)
    cfg = config(remotes, gh, tmp_path)
    first = publish_prs(conn, run, cfg)
    assert publish_prs(conn, run, cfg) == first
    assert len(gh.calls) == 1 and remotes.branches(A) == ["verimend/2026-10-28"]


def test_pr_creation_is_retried_once(conn, remotes, gh, tmp_path) -> None:
    base = remotes.create(A)
    run = make_run(conn, {A: {"commit": base}})
    add_draft(conn, run, A)
    gh.fail_next(1)
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A]["url"] and len(gh.calls) == 2


def test_a_failed_pr_creation_resumes_on_the_pushed_branch(conn, remotes, gh, tmp_path) -> None:
    base = remotes.create(A)
    run = make_run(conn, {A: {"commit": base}})
    verdict = add_draft(conn, run, A)
    cfg = config(remotes, gh, tmp_path)
    gh.fail_next(2)
    prs = publish_prs(conn, run, cfg)
    assert prs[A]["publish_error"]["stage"] == "pr_create" and "server said no" in prs[A]["publish_error"]["detail"]
    assert prs[A]["pushed"] is True and prs[A]["url"] is None
    assert verdict in prs[A]["applied"]
    tip = git(remotes.bare(A), "rev-parse", "verimend/2026-10-28")

    # A dry run cannot finish it, and must not lose the record that a github run needs.
    assert publish_prs(conn, run, config(remotes, gh, tmp_path, mode="dry_run"))[A]["pushed"] is True

    prs = publish_prs(conn, run, cfg)
    assert prs[A]["url"] and prs[A]["branch"] == "verimend/2026-10-28" and "publish_error" not in prs[A]
    assert git(remotes.bare(A), "rev-parse", "verimend/2026-10-28") == tip
    assert remotes.branches(A) == ["verimend/2026-10-28"]  # no second branch for the same run
    assert len(gh.calls) == 3


def test_a_dry_run_then_a_github_run_publishes(conn, remotes, gh, tmp_path) -> None:
    base = remotes.create(A)
    run = make_run(conn, {A: {"commit": base}})
    add_draft(conn, run, A)
    publish_prs(conn, run, config(remotes, gh, tmp_path, mode="dry_run"))
    prs = publish_prs(conn, run, config(remotes, gh, tmp_path))
    assert prs[A]["mode"] == "github" and prs[A]["url"] and "body_path" not in prs[A]


def test_a_later_mender_pass_keeps_the_pr_record(conn) -> None:
    run = make_run(conn, {})
    stats = stored(conn, run)
    stats["mend"] = {"prs": {A: {"url": "u"}}}
    conn.execute("UPDATE crawl_run SET stats_json = ? WHERE id = ?", (canonical_json(stats), run))

    class Client:
        pass

    record_mend(conn, run, MendPass(), MenderSession(Client(), prompt="p"))
    assert stored(conn, run)["mend"]["prs"] == {A: {"url": "u"}}


# ------------------------------------------------------------------ body


def _item(path="README.md", anchor=(6, 6), method="llm", claim="The port is 8113.") -> PrItem:
    return PrItem(path, anchor, claim, method, (Evidence("o/a@abc:src/c.py:4", '{"port":8120}'),))


def test_body_snapshot() -> None:
    body = build_pr_body(
        [
            _item("docs/b.md", (3, 5), "deterministic", "Run `x`."),
            _item("README.md", (9, 9), "llm", "Second."),
            _item("README.md", (2, 2), "llm", "First."),
        ],
        run_id=7, base_commit="abc123",
    )
    assert body == (
        "Verimend run 7 (base `abc123`) が検出した stale な記述の修正案です。\n"
        "\n"
        "## 決定的検証済み\n"
        "\n"
        "- `docs/b.md:3-5`\n"
        "  - クレーム: ``Run `x`.``\n"
        '  - 根拠: `o/a@abc:src/c.py:4` `{"port":8120}`\n'
        "  - 判定方法: deterministic\n"
        "\n"
        "## LLM判定\n"
        "\n"
        "- `README.md:2`\n"
        "  - クレーム: `First.`\n"
        '  - 根拠: `o/a@abc:src/c.py:4` `{"port":8120}`\n'
        "  - 判定方法: llm\n"
        "- `README.md:9`\n"
        "  - クレーム: `Second.`\n"
        '  - 根拠: `o/a@abc:src/c.py:4` `{"port":8120}`\n'
        "  - 判定方法: llm"
    )


def test_body_with_an_empty_section_says_so() -> None:
    body = build_pr_body([_item(method="llm")], run_id=1, base_commit="c")
    assert "## 決定的検証済み\n\nなし\n\n## LLM判定" in body


def test_body_is_cut_at_the_limit_and_counts_what_it_left_out() -> None:
    items = [_item(anchor=(n, n), claim="x" * 200) for n in range(1, 1001)]
    body = build_pr_body(items, run_id=1, base_commit="c")
    assert len(body) <= 60_000
    shown = body.count("判定方法: llm")
    assert 0 < shown < 1000 and body.endswith(f"\n\nほか {1000 - shown} 件")
    assert len(build_pr_body(items[:3], run_id=1, base_commit="c", limit=500)) <= 500


def test_body_keeps_mentions_and_closing_keywords_inside_code_spans() -> None:
    body = build_pr_body([_item(claim="ping @someone, fixes #12\nand `tick`")], run_id=1, base_commit="c")
    assert "`` ping @someone, fixes #12 and `tick` ``" in body


# ----------------------------------------------------------------- guards


@pytest.mark.parametrize(
    ("original", "replacement", "expected"),
    [
        (["port 8113  "], ["port 8113"], "no_change"),
        (["port 8113"], ["port 8113\t "], "no_change"),
        (["a  ", "b"], ["a", "b  "], "no_change"),
        (["  port 8113"], ["port 8113"], None),  # leading whitespace can matter
        (["port 8113  "], ["port 8120"], None),
    ],
)
def test_a_trailing_whitespace_only_draft_is_no_change(original, replacement, expected) -> None:
    assert check_replacement(original, replacement) == expected


def test_settings_default_to_dry_run_and_refuse_a_bad_mode_or_author(monkeypatch) -> None:
    for name in ("MEND_PUBLISH", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GH_BIN"):
        monkeypatch.delenv(f"VERIMEND_{name}", raising=False)
    settings = Settings()
    assert settings.mend_publish == "dry_run" and settings.gh_bin == "gh"
    with pytest.raises(ValidationError):
        Settings(mend_publish="live")
    with pytest.raises(ValidationError):
        Settings(git_author_name=" ")
    with pytest.raises(ValidationError):
        Settings(git_author_email="")


def test_targets_take_an_optional_pr_base() -> None:
    target = {"repo": A, "doc_globs": ["README.md"], "extractors": {"ports": True}}
    assert parse_targets({"version": 1, "targets": [target]}).targets[0].pr_base is None
    assert parse_targets({"version": 1, "targets": [target | {"pr_base": "dev"}]}).targets[0].pr_base == "dev"


# ---------------------------------------------------------------- pipeline


def _pipeline_targets():
    return parse_targets(
        {"version": 1, "targets": [{"repo": A, "doc_globs": ["README.md"], "extractors": {"ports": True}}]}
    ).targets



def _stub_stages(monkeypatch, run_mender) -> None:
    from verimend import pipeline

    monkeypatch.setattr(pipeline, "extract_documents", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "reconcile", lambda *a: None)
    monkeypatch.setattr(pipeline, "_mend", run_mender)


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _checkout(tmp_path, write_repo):
    source = tmp_path / "source"
    commit = write_repo(source, {"README.md": "# x\n", "a.py": "PORT = 1\n"})

    def checkout(repo, dest):
        git(tmp_path, "clone", "-q", str(source), str(dest))
        return commit

    return checkout


def test_the_pipeline_publishes_after_the_mender_and_a_failure_makes_it_partial(
    conn, monkeypatch, tmp_path, write_repo
) -> None:
    order = []
    _stub_stages(monkeypatch, lambda *a: order.append("mend"))

    def publish(c, run_id):
        order.append("publish")
        stats = stored(c, run_id)
        stats.setdefault("mend", {})["prs"] = {A: {"publish_error": {"stage": "push", "detail": "d"}}}
        c.execute("UPDATE crawl_run SET stats_json = ? WHERE id = ?", (canonical_json(stats), run_id))
        c.commit()

    result = run_pipeline(
        conn, _pipeline_targets(), _checkout(tmp_path, write_repo), _Session,
        make_mender=lambda: None, publish=publish,
    )
    assert order == ["mend", "publish"] and result.status == "partial"
    assert result.stats["mend"]["prs"][A]["publish_error"]["stage"] == "push"


def test_without_a_mender_nothing_is_published(conn, monkeypatch, tmp_path, write_repo) -> None:
    _stub_stages(monkeypatch, lambda *a: None)
    called = []
    result = run_pipeline(
        conn, _pipeline_targets(), _checkout(tmp_path, write_repo), _Session,
        publish=lambda *a: called.append(a),
    )
    assert called == [] and result.status == "succeeded"


def test_an_unexpected_publish_error_keeps_the_recorded_prs(conn, monkeypatch, tmp_path, write_repo) -> None:
    _stub_stages(monkeypatch, lambda *a: None)

    def publish(c, run_id):
        stats = stored(c, run_id)
        stats.setdefault("mend", {})["prs"] = {A: {"url": "https://github.example/pull/1"}}
        c.execute("UPDATE crawl_run SET stats_json = ? WHERE id = ?", (canonical_json(stats), run_id))
        c.commit()
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run_pipeline(
            conn, _pipeline_targets(), _checkout(tmp_path, write_repo), _Session,
            make_mender=lambda: None, publish=publish,
        )
    row = conn.execute("SELECT status, stats_json FROM crawl_run").fetchone()
    assert row[0] == "failed" and json.loads(row[1])["mend"]["prs"][A]["url"]


# --------------------------------------------------------------------- CLI


def _cli_env(monkeypatch, tmp_path, remotes, gh, mode) -> Path:
    targets = tmp_path / "targets.yaml"
    targets.write_text(
        f"version: 1\ntargets:\n  - repo: {A}\n    doc_globs: [README.md]\n    extractors: {{ports: true}}\n",
        encoding="utf-8",
    )
    db = tmp_path / "cli.sqlite3"
    monkeypatch.setenv("VERIMEND_DB_PATH", str(db))
    monkeypatch.setenv("VERIMEND_TARGETS_PATH", str(targets))
    monkeypatch.setenv("VERIMEND_GITHUB_BASE_URL", remotes.url)
    monkeypatch.setenv("VERIMEND_GH_BIN", str(gh.bin))
    monkeypatch.setenv("VERIMEND_MEND_PUBLISH", mode)
    monkeypatch.setenv("VERIMEND_MEND_ARTEFACT_DIR", str(tmp_path / "artefacts"))
    migrate(db)
    return db


def test_cli_publish_resumes_a_run_and_reports_by_exit_code(monkeypatch, tmp_path, remotes, gh, capsys) -> None:
    db = _cli_env(monkeypatch, tmp_path, remotes, gh, "github")
    base = remotes.create(A)
    remotes.reject_pushes(A)
    with connection(db) as c:
        run = make_run(c, {A: {"commit": base}})
        add_draft(c, run, A)
    assert cli.main(["publish", "--run", str(run)]) == 1
    remotes.accept_pushes(A)
    assert cli.main(["publish", "--run", str(run)]) == 0
    assert f"https://github.example/{A}/pull/1" in capsys.readouterr().out
    assert cli.main(["publish", "--run", "999"]) == 2


def test_cli_run_defaults_to_a_dry_run_end_to_end(monkeypatch, tmp_path, remotes, gh) -> None:
    from extractor_fakes import FakeClient, claims, port_claim, session

    db = _cli_env(monkeypatch, tmp_path, remotes, gh, "dry_run")
    monkeypatch.delenv("VERIMEND_LEXORA_URL", raising=False)
    remotes.create(A, "# Title\n\nThe port is 8120.\n")
    (remotes.root / "work" / A / "app.py").write_text("PORT = 8120\n", encoding="utf-8")
    work = remotes.root / "work" / A
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "code")
    git(work, "push", "-q", str(remotes.bare(A)), "HEAD:trunk")

    from test_pipeline import StaleJudge, _judge, _mender

    monkeypatch.setattr(
        cli, "session_from_settings",
        lambda s: session(FakeClient(by_chunk=lambda start: claims(port_claim(3, 8120, polarity="historical"))))[0],
    )
    monkeypatch.setattr(cli, "judge_from_settings", lambda s: _judge(StaleJudge()))
    monkeypatch.setattr(cli, "mender_from_settings", lambda s: _mender(FakeClient(json.dumps({"lines": ["fixed"]}))))
    assert cli.main(["run"]) == 0
    assert remotes.branches(A) == [] and gh.calls == []
    with connection(db) as c:
        status, stats_json = c.execute("SELECT status, stats_json FROM crawl_run").fetchone()
    pr = json.loads(stats_json)["mend"]["prs"][A]
    assert status == "succeeded" and pr["mode"] == "dry_run" and len(pr["applied"]) == 1 and pr["skipped"] == []
