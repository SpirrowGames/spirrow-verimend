"""The mender's second stage (docs/design.md 5.4, M3b): patch drafts become one branch and PR per repository.

``publish_prs(conn, run_id, config)`` works from the database alone (drafts, claims,
facts, ``stats.targets``), so a run can be published again without any LLM call.

The record is ``stats.mend.prs``, a dict keyed by ``doc_repo``. It is the source of
truth for a run's pull requests; ``crawl_run.pr_url`` holds one value and stays NULL.
Each repository's record is committed as soon as it is known, so a later repository's
failure cannot lose it, and a repository whose ``url`` is recorded is never published
again.

Per repository:

- **Base.** ``stats.targets[<repo>].commit``, the commit the facts and the anchors
  came from. Without one the repository is not published (``no_base_commit``): the
  anchors mean nothing on another commit, so the default branch is no substitute.
- **Worktree.** A fresh temporary directory fetching that commit, removed on every path.
- **Apply.** Each draft must pass a strict ``git apply --check`` against the untouched
  base. Drafts whose anchors overlap cannot both stand, so the later one in the order
  below loses. Both losses are ``apply_conflict``, for that draft only. The rest is
  applied by ``doc_path``, then anchor start descending, so a line number is still
  right when its draft's turn comes. That step uses ``-C0`` because a neighbouring
  draft may already have changed this one's context lines; the strict check has
  already verified that context against the base.
- **Publish.** Only ``mode == "github"`` pushes and runs ``gh pr create``. ``dry_run``
  takes the same path up to the local commit, then writes the body to a file; the
  commit goes with the worktree. A push is never forced: a taken branch name gets
  ``-r<run_id>``. A push or PR creation is tried twice. A repository that was pushed
  but has no PR resumes at PR creation on its recorded branch.

Failures stay with their repository (``publish_error = {stage, detail}``, stage
``worktree`` | ``push`` | ``pr_create``) and make the run ``partial``, as
``no_base_commit`` does. Drafts and verdicts are never changed here.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verimend.collector.facts import canonical_json

log = logging.getLogger(__name__)

MODE_DRY_RUN = "dry_run"
MODE_GITHUB = "github"

REASON_NO_DRAFTS = "no_drafts"
REASON_ALL_SKIPPED = "all_skipped"
REASON_NO_BASE_COMMIT = "no_base_commit"
SKIP_APPLY_CONFLICT = "apply_conflict"

STAGE_WORKTREE = "worktree"
STAGE_PUSH = "push"
STAGE_PR_CREATE = "pr_create"

BODY_MAX_CHARS = 60_000
FACT_SUMMARY_MAX = 200
DETAIL_MAX = 500
SECTIONS = (("deterministic", "決定的検証済み"), ("llm", "LLM判定"))


@dataclass(frozen=True)
class PublishConfig:
    mode: str
    author_name: str
    author_email: str
    gh_bin: str
    github_base_url: str
    timeout_s: float
    artefact_dir: Path
    pr_bases: Mapping[str, str | None] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Any, targets: Sequence[Any]) -> "PublishConfig":
        return cls(
            mode=settings.mend_publish,
            author_name=settings.git_author_name,
            author_email=settings.git_author_email,
            gh_bin=settings.gh_bin,
            github_base_url=settings.github_base_url,
            timeout_s=settings.clone_timeout_s,
            artefact_dir=settings.mend_artefact_dir,
            pr_bases={t.repo: t.pr_base for t in targets},
        )


@dataclass(frozen=True)
class Evidence:
    source_ref: str
    summary: str


@dataclass(frozen=True)
class PrItem:
    """One drafted stale verdict, as the PR body lists it."""

    doc_path: str
    anchor: tuple[int, int]
    claim_text: str
    method: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class _Draft:
    verdict_id: int
    patch: str
    item: PrItem


class _StageError(Exception):
    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


# ----------------------------------------------------------------- PR body


def build_pr_body(items: Sequence[PrItem], *, run_id: int, base_commit: str, limit: int = BODY_MAX_CHARS) -> str:
    """The PR body: pure, at most ``limit`` characters, whatever does not fit counted as ``ほか N 件``.

    Text taken from a document or a fact goes in code spans, so a mention, an issue
    reference or a closing keyword inside it has no effect on GitHub.
    """
    ordered = {
        method: sorted((i for i in items if i.method == method), key=lambda i: (i.doc_path, i.anchor))
        for method, _ in SECTIONS
    }
    total = sum(len(group) for group in ordered.values())
    used = f"Verimend run {run_id} (base `{base_commit}`) が検出した stale な記述の修正案です。"
    parts = [used]
    length = len(used)
    reserve = len(_omitted(total))
    included = 0
    for method, title in SECTIONS:
        heading = f"\n\n## {title}\n"
        empty = "\nなし"
        lines: list[str] = []
        length += len(heading) + len(empty)  # kept reserved even if the section turns out non-empty
        for item in ordered[method]:
            entry = _render_item(item)
            if length + len(entry) + reserve > limit:
                continue
            lines.append(entry)
            length += len(entry)
            included += 1
        parts.append(heading + ("".join(lines) if lines else empty))
    if included < total:
        parts.append(_omitted(total - included))
    return "".join(parts)


def _omitted(count: int) -> str:
    return f"\n\nほか {count} 件"


def _render_item(item: PrItem) -> str:
    start, end = item.anchor
    where = f"{item.doc_path}:{start}" if start == end else f"{item.doc_path}:{start}-{end}"
    out = [f"\n- {_code(where)}\n  - クレーム: {_code(item.claim_text)}"]
    out += [f"\n  - 根拠: {_code(e.source_ref)} {_code(e.summary)}" for e in item.evidence]
    out.append(f"\n  - 判定方法: {item.method}")
    return "".join(out)


def _code(text: str) -> str:
    """``text`` as one inline code span, on one line, whatever backticks it holds."""
    flat = " ".join(text.split())
    longest = run = 0
    for char in flat:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * (longest + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") else ""
    return f"{fence}{pad}{flat}{pad}{fence}"


# ------------------------------------------------------------------- stage


def publish_prs(conn: sqlite3.Connection, run_id: int, config: PublishConfig) -> dict[str, Any]:
    """Publish every repository of the run that has no recorded PR; return ``stats.mend.prs``."""
    started_at, stats = _run_row(conn, run_id)
    targets = stats.get("targets", {})
    drafts = _drafts(conn, run_id)
    for repo in sorted(set(targets) | set(drafts)):
        prior = _run_row(conn, run_id)[1].get("mend", {}).get("prs", {}).get(repo, {})
        if prior.get("url"):
            log.info("%s: already published at %s", repo, prior["url"])
            continue
        if prior.get("pushed") and config.mode != MODE_GITHUB:
            # Its record is what lets a github run finish the PR; a dry run must not replace it.
            log.info("%s: branch %s is pushed but has no PR; left for a github run", repo, prior.get("branch"))
            continue
        record = _publish_repo(run_id, started_at, repo, targets.get(repo, {}), drafts.get(repo, []), prior, config)
        _record(conn, run_id, repo, record)
    return _run_row(conn, run_id)[1].get("mend", {}).get("prs", {})


def _publish_repo(
    run_id: int,
    started_at: str,
    repo: str,
    target: Mapping[str, Any],
    drafts: Sequence[_Draft],
    prior: Mapping[str, Any],
    config: PublishConfig,
) -> dict[str, Any]:
    if not drafts:
        return {"mode": config.mode, "reason": REASON_NO_DRAFTS}
    base_commit = target.get("commit")
    if not base_commit:
        log.error("%s: %d drafts but no recorded commit; not published", repo, len(drafts))
        return {"mode": config.mode, "reason": REASON_NO_BASE_COMMIT}
    record: dict[str, Any] = {"mode": config.mode, "base_commit": base_commit, "url": None}
    if prior.get("pushed"):
        record.update(branch=prior["branch"], pushed=True)
    try:
        with tempfile.TemporaryDirectory(prefix="verimend-mend-") as tmp:
            _publish_in_worktree(Path(tmp) / "repo", run_id, started_at, repo, base_commit, drafts, record, config)
    except _StageError as exc:
        log.error("%s: publishing failed at %s: %s", repo, exc.stage, exc.detail)
        record["publish_error"] = {"stage": exc.stage, "detail": exc.detail}
    return record


def _publish_in_worktree(
    worktree: Path,
    run_id: int,
    started_at: str,
    repo: str,
    base_commit: str,
    drafts: Sequence[_Draft],
    record: dict[str, Any],
    config: PublishConfig,
) -> None:
    git = _Git(worktree, config)
    date = _utc_date(started_at)
    try:
        worktree.mkdir()
        git("init", "-q")
        git("remote", "add", "origin", f"{config.github_base_url.rstrip('/')}/{repo}.git")
        git("fetch", "-q", "--depth", "1", "--no-tags", "origin", base_commit)
        git("checkout", "-q", "--detach", "FETCH_HEAD")
        applied, skipped = _apply(git, drafts)
        record.update(applied=[d.verdict_id for d in applied], skipped=skipped)
        if not applied:
            record.clear()
            record.update(mode=config.mode, reason=REASON_ALL_SKIPPED, skipped=skipped)
            return
        items = [d.item for d in applied]
        body = build_pr_body(items, run_id=run_id, base_commit=base_commit)
        pr_base = config.pr_bases.get(repo) or _default_branch(git)
        if "branch" not in record:
            branch = f"verimend/{date}"
            if git("ls-remote", "--heads", "origin", f"refs/heads/{branch}").strip():
                branch = f"{branch}-r{run_id}"
            record["branch"] = branch
            git("checkout", "-q", "-b", branch)
            git("add", "-A")
            git(
                "-c", f"user.name={config.author_name}", "-c", f"user.email={config.author_email}",
                "-c", "commit.gpgsign=false",
                "commit", "-q", "-m", f"{_title(date)}\n\nVerimend run {run_id}, {len(applied)} 件。",
            )
        if config.mode != MODE_GITHUB:
            record["body_path"] = str(_write_body(config.artefact_dir, run_id, repo, body))
            return
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise _StageError(STAGE_WORKTREE, _describe(exc)) from exc

    if not record.get("pushed"):
        _twice(STAGE_PUSH, lambda: git("push", "-q", "origin", f"HEAD:refs/heads/{record['branch']}"))
        record["pushed"] = True
    record["url"] = _twice(STAGE_PR_CREATE, lambda: _gh_pr_create(repo, pr_base, record["branch"], date, body, config))


def _apply(git: "_Git", drafts: Sequence[_Draft]) -> tuple[list[_Draft], list[dict[str, Any]]]:
    """Apply what can be applied to the worktree; return ``(applied, skipped)``. See the module docstring."""
    order = sorted(drafts, key=lambda d: (d.item.doc_path, -d.item.anchor[0], d.verdict_id))
    applied: list[_Draft] = []
    skipped: list[dict[str, Any]] = []

    def skip(draft: _Draft) -> None:
        log.warning("verdict %s: %s", draft.verdict_id, SKIP_APPLY_CONFLICT)
        skipped.append({"verdict_id": draft.verdict_id, "reason": SKIP_APPLY_CONFLICT})

    clean = []
    for draft in order:  # against the untouched base: nothing is applied until this loop ends
        if git.apply(draft.patch, "--check"):
            clean.append(draft)
        else:
            skip(draft)
    for draft in clean:
        if any(_overlaps(draft.item, other.item) for other in applied) or not git.apply(draft.patch, "-C0"):
            skip(draft)
        else:
            applied.append(draft)
    return applied, skipped


def _overlaps(a: PrItem, b: PrItem) -> bool:
    return a.doc_path == b.doc_path and a.anchor[0] <= b.anchor[1] and b.anchor[0] <= a.anchor[1]


def _gh_pr_create(repo: str, base: str, branch: str, date: str, body: str, config: PublishConfig) -> str:
    done = subprocess.run(
        [config.gh_bin, "pr", "create", "--repo", repo, "--base", base, "--head", branch,
         "--title", _title(date), "--body-file", "-"],
        input=body.encode("utf-8"), check=True, capture_output=True, timeout=config.timeout_s,
        env={**os.environ, "GH_PROMPT_DISABLED": "1"},
    )
    lines = done.stdout.decode("utf-8", "replace").strip().splitlines()
    url = lines[-1].strip() if lines else ""
    if not url.startswith("http"):
        raise OSError(f"gh pr create printed no URL: {url!r}")
    return url


def _twice(stage: str, action):
    """``action()``, retried once; the second failure is ``_StageError(stage)``."""
    try:
        return action()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as first:
        log.warning("%s failed, retrying once: %s", stage, _describe(first))
    try:
        return action()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise _StageError(stage, _describe(exc)) from exc


class _Git:
    def __init__(self, root: Path, config: PublishConfig) -> None:
        self.root = root
        self.timeout = config.timeout_s
        self.env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def __call__(self, *args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(self.root), *args], check=True, capture_output=True, timeout=self.timeout, env=self.env
        )
        return done.stdout.decode("utf-8", "replace")

    def apply(self, patch: str, *flags: str) -> bool:
        """Whether ``git apply`` took the patch. Refusing it is an answer, not an error."""
        done = subprocess.run(
            ["git", "-C", str(self.root), "apply", "--whitespace=nowarn", *flags, "-"],
            input=patch.encode("utf-8"), capture_output=True, timeout=self.timeout, env=self.env,
        )
        return done.returncode == 0


def _default_branch(git: _Git) -> str:
    for line in git("ls-remote", "--symref", "origin", "HEAD").splitlines():
        if line.startswith("ref: refs/heads/"):
            return line.split("\t")[0].removeprefix("ref: refs/heads/")
    raise OSError("the remote reports no default branch")


def _write_body(artefact_dir: Path, run_id: int, repo: str, body: str) -> Path:
    path = Path(artefact_dir) / f"run-{run_id}" / f"{repo.replace('/', '__')}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def _title(date: str) -> str:
    return f"docs: Verimend によるドキュメント修正 ({date})"


def _utc_date(started_at: str) -> str:
    return datetime.fromisoformat(started_at).astimezone(timezone.utc).strftime("%Y-%m-%d")


def _describe(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        lines = stderr.strip().splitlines()
        detail = f"exit {exc.returncode}: {lines[-1] if lines else exc.cmd[0]}"
    elif isinstance(exc, subprocess.TimeoutExpired):
        detail = f"timed out after {exc.timeout}s"
    else:
        detail = f"{exc.__class__.__name__}: {exc}"
    return detail[:DETAIL_MAX]


# ---------------------------------------------------------------- database


def _run_row(conn: sqlite3.Connection, run_id: int) -> tuple[str, dict[str, Any]]:
    started_at, stats_json = conn.execute(
        "SELECT started_at, stats_json FROM crawl_run WHERE id = ?", (run_id,)
    ).fetchone()
    return started_at, json.loads(stats_json or "{}")


def _record(conn: sqlite3.Connection, run_id: int, repo: str, record: dict[str, Any]) -> None:
    _, stats = _run_row(conn, run_id)
    stats.setdefault("mend", {}).setdefault("prs", {})[repo] = record
    conn.execute("UPDATE crawl_run SET stats_json = ? WHERE id = ?", (canonical_json(stats), run_id))
    conn.commit()


def _drafts(conn: sqlite3.Connection, run_id: int) -> dict[str, list[_Draft]]:
    rows = conn.execute(
        """
        SELECT v.id, v.patch_draft, v.method, v.evidence_json, c.doc_repo, c.doc_path, c.anchor_start, c.anchor_end, c.text
        FROM verdict AS v JOIN claim AS c ON c.id = v.claim_id
        WHERE c.run_id = ? AND v.result = 'stale' AND v.patch_draft IS NOT NULL
        ORDER BY v.id
        """,
        (run_id,),
    ).fetchall()
    out: dict[str, list[_Draft]] = {}
    for verdict_id, patch, method, evidence_json, repo, path, start, end, text in rows:
        item = PrItem(path, (start, end), text, method, _evidence(conn, run_id, evidence_json))
        out.setdefault(repo, []).append(_Draft(verdict_id, patch, item))
    return out


def _evidence(conn: sqlite3.Connection, run_id: int, evidence_json: str) -> tuple[Evidence, ...]:
    ids = sorted(set(json.loads(evidence_json)["fact_ids"]))  # a draft exists only for usable evidence (mend_stale)
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT source_ref, content FROM fact WHERE run_id = ? AND id IN ({marks}) ORDER BY id", (run_id, *ids)
    ).fetchall()
    return tuple(Evidence(ref, _summary(content)) for ref, content in rows)


def _summary(content: str) -> str:
    return content if len(content) <= FACT_SUMMARY_MAX else content[:FACT_SUMMARY_MAX] + "..."
