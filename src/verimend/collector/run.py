"""One collection run: check out each target, run its extractors, store the facts.

Failure is contained at the smallest unit that can fail on its own:

- an extractor that raises loses only its own facts (nothing it produced is
  stored -- a half-extracted set would read as "these tools no longer exist");
- a target that cannot be checked out loses only that target;
- either way the run finishes as ``partial`` with the reason in ``stats_json``.

Only an error outside those units (the database itself, say) marks the run
``failed`` and propagates.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verimend.collector.extractors import REGISTRY
from verimend.collector.facts import canonical_json
from verimend.collector.tree import RepoTree
from verimend.targets import Target

log = logging.getLogger(__name__)

# (repo "owner/name", destination directory) -> checked-out commit SHA
Checkout = Callable[[str, Path], str]

STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class CollectResult:
    run_id: int
    status: str
    stats: dict[str, Any]


def github_checkout(base_url: str = "https://github.com", timeout: float = 300.0) -> Checkout:
    """A ``Checkout`` doing a depth-1 clone of ``<base_url>/<repo>.git``.

    Authentication is whatever git on the host is configured with (credential
    helper). ``GIT_TERMINAL_PROMPT=0`` makes a missing credential an error
    instead of a prompt that would hang an unattended run, and ``timeout``
    bounds a connect that never completes.
    """

    def checkout(repo: str, dest: Path) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        url = f"{base_url.rstrip('/')}/{repo}.git"
        subprocess.run(
            ["git", "clone", "--depth", "1", "--no-tags", "--quiet", url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return subprocess.run(
            ["git", "-C", str(dest), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    return checkout


def collect(conn: sqlite3.Connection, targets: Sequence[Target], checkout: Checkout) -> CollectResult:
    """Record a ``crawl_run`` and the facts of every target. Returns its outcome."""
    run_id = conn.execute(
        "INSERT INTO crawl_run (started_at, status) VALUES (?, ?)",
        (_now(), STATUS_RUNNING),
    ).lastrowid
    conn.commit()

    stats: dict[str, Any] = {"targets": {}}
    try:
        for target in targets:
            stats["targets"][target.repo] = _collect_target(conn, run_id, target, checkout)
            conn.commit()
    except BaseException:
        conn.rollback()
        _finish(conn, run_id, STATUS_FAILED, stats)
        raise

    status = STATUS_PARTIAL if _has_errors(stats) else STATUS_SUCCEEDED
    _finish(conn, run_id, status, stats)
    return CollectResult(run_id=run_id, status=status, stats=stats)


def _collect_target(conn: sqlite3.Connection, run_id: int, target: Target, checkout: Checkout) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="verimend-") as tmp:
        dest = Path(tmp) / "repo"
        try:
            commit = checkout(target.repo, dest)
            tree = RepoTree.from_checkout(dest)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            log.error("checkout of %s failed: %s", target.repo, _describe(exc))
            return {"error": f"checkout failed: {_describe(exc)}"}

        result: dict[str, Any] = {"commit": commit, "extractors": {}}
        for name in target.enabled_extractors:
            extractor = REGISTRY.get(name)
            if extractor is None:
                result["extractors"][name.value] = {"skipped": "not implemented yet"}
                continue
            try:
                facts = list(extractor(tree))
            except Exception as exc:  # noqa: BLE001 - contained per extractor, see module docstring
                log.exception("extractor %s failed on %s", name.value, target.repo)
                result["extractors"][name.value] = {"error": f"{exc.__class__.__name__}: {exc}"}
                continue
            conn.executemany(
                "INSERT INTO fact (run_id, source_kind, source_ref, content, content_hash) VALUES (?, ?, ?, ?, ?)",
                [
                    (run_id, f.source_kind, f.source_ref(target.repo, commit), f.canonical_content(), f.content_hash())
                    for f in facts
                ],
            )
            result["extractors"][name.value] = {"facts": len(facts)}
        result["skipped_files"] = dict(sorted(tree.skipped.items()))
        return result


def _finish(conn: sqlite3.Connection, run_id: int, status: str, stats: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE crawl_run SET finished_at = ?, status = ?, stats_json = ? WHERE id = ?",
        (_now(), status, canonical_json(stats), run_id),
    )
    conn.commit()


def _has_errors(stats: dict[str, Any]) -> bool:
    for target in stats["targets"].values():
        if "error" in target:
            return True
        if any("error" in e for e in target.get("extractors", {}).values()):
            return True
    return False


def _describe(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or "").strip().splitlines()
        return f"exit {exc.returncode}: {detail[-1] if detail else exc.cmd}"
    return f"{exc.__class__.__name__}: {exc}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
