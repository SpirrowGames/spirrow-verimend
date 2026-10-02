"""Command line entry point: ``verimend serve`` / ``verimend migrate`` / ``verimend collect``."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence

from verimend.collector import collect
from verimend.collector.health import magickit_health_source
from verimend.collector.run import STATUS_FAILED, STATUS_PARTIAL, github_checkout
from verimend.db import connection
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
    args = parser.parse_args(argv)

    settings = get_settings()

    if args.command == "migrate":
        applied = run_migrations(settings.db_path)
        print(f"{settings.db_path}: applied {len(applied)} migration(s): {', '.join(applied) or 'none'}")
        return 0

    if args.command == "collect":
        return _collect(settings, args.repo)

    import uvicorn  # imported lazily so `verimend migrate` does not need the server stack

    uvicorn.run("verimend.app:app", host=settings.host, port=settings.port)
    return 0


def _collect(settings, repos: list[str] | None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_targets(settings.targets_path)
    targets = config.targets
    if repos:
        unknown = [r for r in repos if config.get(r) is None]
        if unknown:
            print(f"not a target in {settings.targets_path}: {', '.join(unknown)}")
            return 2
        targets = [t for t in targets if t.repo in repos]

    run_migrations(settings.db_path)
    checkout = github_checkout(settings.github_base_url, settings.clone_timeout_s)
    with connection(settings.db_path) as conn:
        result = collect(
            conn, targets, checkout, magickit_health_source(settings.magickit_url, settings.magickit_timeout_s)
        )
    print(f"crawl_run {result.run_id}: {result.status}")
    print(json.dumps(result.stats, indent=2, ensure_ascii=False))
    return {STATUS_PARTIAL: 1, STATUS_FAILED: 1}.get(result.status, 0)


if __name__ == "__main__":
    raise SystemExit(main())
