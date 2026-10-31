"""Command line entry point: ``verimend serve`` / ``migrate`` / ``collect`` / ``run`` / ``publish``."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence

from verimend.collector import collect
from verimend.collector.health import magickit_health_source
from verimend.collector.run import STATUS_FAILED, STATUS_PARTIAL, github_checkout
from verimend.db import connection
from verimend.extractor import session_from_settings
from verimend.mender import PublishConfig, mend_stage_failed, mender_from_settings, publish_prs
from verimend.pipeline import run_pipeline
from verimend.reconciler import judge_from_settings
from verimend.db import migrate as run_migrations
from verimend.settings import get_settings
from verimend.targets import load_targets


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verimend", description="Verimend service")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the HTTP service (default)")
    sub.add_parser("migrate", help="apply pending SQLite migrations and exit")
    collect_parser = sub.add_parser("collect", help="collect facts from the crawl targets and exit")
    collect_parser.add_argument(
        "--repo",
        action="append",
        metavar="OWNER/NAME",
        help="limit to this target (repeatable); default is every target in targets.yaml",
    )
    run_parser = sub.add_parser(
        "run", help="collect, extract claims and reconcile them (checkers, then the Lexora judge) in one crawl_run, then exit"
    )
    run_parser.add_argument(
        "--repo",
        action="append",
        metavar="OWNER/NAME",
        help="limit to this target (repeatable); default is every target in targets.yaml",
    )
    publish_parser = sub.add_parser(
        "publish", help="publish a run's patch drafts again (VERIMEND_MEND_PUBLISH); repositories with a PR are skipped"
    )
    publish_parser.add_argument("--run", type=int, required=True, metavar="ID", help="the crawl_run to publish")
    args = parser.parse_args(argv)

    settings = get_settings()

    if args.command == "migrate":
        applied = run_migrations(settings.db_path)
        print(f"{settings.db_path}: applied {len(applied)} migration(s): {', '.join(applied) or 'none'}")
        return 0

    if args.command == "collect":
        return _collect(settings, args.repo)

    if args.command == "run":
        return _run(settings, args.repo)

    if args.command == "publish":
        return _publish(settings, args.run)

    import uvicorn  # imported lazily so `verimend migrate` does not need the server stack

    uvicorn.run("verimend.app:app", host=settings.host, port=settings.port)
    return 0


def _targets(settings, repos: list[str] | None):
    config = load_targets(settings.targets_path)
    targets = config.targets
    if repos:
        unknown = [r for r in repos if config.get(r) is None]
        if unknown:
            print(f"not a target in {settings.targets_path}: {', '.join(unknown)}")
            return None
        targets = [t for t in targets if t.repo in repos]
    return targets


def _collect(settings, repos: list[str] | None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    targets = _targets(settings, repos)
    if targets is None:
        return 2

    run_migrations(settings.db_path)
    checkout = github_checkout(settings.github_base_url, settings.clone_timeout_s)
    with connection(settings.db_path) as conn:
        result = collect(
            conn, targets, checkout, magickit_health_source(settings.magickit_url, settings.magickit_timeout_s)
        )
    print(f"crawl_run {result.run_id}: {result.status}")
    print(json.dumps(result.stats, indent=2, ensure_ascii=False))
    return {STATUS_PARTIAL: 1, STATUS_FAILED: 1}.get(result.status, 0)


def _run(settings, repos: list[str] | None) -> int:
    """The whole pipeline. Exit 0 = succeeded, 1 = partial or failed (a configuration error is ``failed``)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    targets = _targets(settings, repos)
    if targets is None:
        return 2

    run_migrations(settings.db_path)
    checkout = github_checkout(settings.github_base_url, settings.clone_timeout_s)
    with connection(settings.db_path) as conn:
        result = run_pipeline(
            conn,
            targets,
            checkout,
            lambda: session_from_settings(settings),
            magickit_health_source(settings.magickit_url, settings.magickit_timeout_s),
            make_judge=lambda: judge_from_settings(settings),
            make_mender=lambda: mender_from_settings(settings),
            publish=lambda c, run_id: publish_prs(c, run_id, PublishConfig.from_settings(settings, targets)),
        )
    print(f"crawl_run {result.run_id}: {result.status}")
    print(json.dumps(result.stats, indent=2, ensure_ascii=False))
    return {STATUS_PARTIAL: 1, STATUS_FAILED: 1}.get(result.status, 0)


def _publish(settings, run_id: int) -> int:
    """Publish one existing run. Exit 0 = every repository settled, 1 = one was not, 2 = no such run.

    The run's status stays as it finished: a retry that succeeds shows in ``stats.mend.prs``.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    run_migrations(settings.db_path)
    config = PublishConfig.from_settings(settings, load_targets(settings.targets_path).targets)
    with connection(settings.db_path) as conn:
        if conn.execute("SELECT 1 FROM crawl_run WHERE id = ?", (run_id,)).fetchone() is None:
            print(f"no crawl_run {run_id}")
            return 2
        prs = publish_prs(conn, run_id, config)
    print(json.dumps(prs, indent=2, ensure_ascii=False))
    return 1 if mend_stage_failed({"prs": prs}) else 0


if __name__ == "__main__":
    raise SystemExit(main())
