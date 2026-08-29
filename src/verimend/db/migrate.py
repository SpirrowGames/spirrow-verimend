"""Forward-only SQLite migration runner.

Each migration is a ``NNNN_name.sql`` file in ``verimend/db/migrations``,
applied in filename order inside a single transaction and recorded in the
``schema_migration`` bookkeeping table. Applying an already-applied migration
is a no-op, so ``apply_migrations`` is idempotent: running it against an
up-to-date database returns an empty list and changes nothing.

Only the tables the M1 collector actually writes exist so far (``crawl_run``
and ``fact``). ``claim`` / ``verdict`` / ``metric`` from docs/design.md
section 4 arrive as later migrations, when the code that writes them does.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

BOOKKEEPING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection, creating the parent directory when needed."""
    path = Path(db_path)
    if path.parent and str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _available() -> list[tuple[str, Path]]:
    return [(p.stem, p) for p in sorted(MIGRATIONS_DIR.glob("*.sql"))]


def _applied(conn: sqlite3.Connection) -> set[str]:
    conn.executescript(BOOKKEEPING_DDL)
    return {row["version"] for row in conn.execute("SELECT version FROM schema_migration")}


def pending_migrations(conn: sqlite3.Connection) -> list[str]:
    """Versions present on disk but not yet recorded in the database."""
    applied = _applied(conn)
    return [version for version, _ in _available() if version not in applied]


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply every pending migration. Returns the versions applied, in order."""
    applied = _applied(conn)
    newly_applied: list[str] = []
    for version, path in _available():
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migration (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(timezone.utc).isoformat()),
            )
        newly_applied.append(version)
    return newly_applied


def migrate(db_path: Path | str) -> list[str]:
    """Open ``db_path`` and bring it up to date. Returns the versions applied."""
    with connect(db_path) as conn:
        return apply_migrations(conn)
