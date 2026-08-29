"""Command line entry point: ``verimend serve`` / ``verimend migrate``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from verimend.db import migrate as run_migrations
from verimend.settings import get_settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verimend", description="Verimend service")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the HTTP service (default)")
    sub.add_parser("migrate", help="apply pending SQLite migrations and exit")
    args = parser.parse_args(argv)

    settings = get_settings()

    if args.command == "migrate":
        applied = run_migrations(settings.db_path)
        print(f"{settings.db_path}: applied {len(applied)} migration(s): {', '.join(applied) or 'none'}")
        return 0

    import uvicorn  # imported lazily so `verimend migrate` does not need the server stack

    uvicorn.run("verimend.app:app", host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
