"""Forward-only SQLite migration runner.

Each migration is a ``NNNN_name.sql`` file in ``verimend/db/migrations``,
applied in filename order and recorded in the ``schema_migration``
bookkeeping table. An already-recorded migration is skipped, so
``apply_migrations`` is idempotent: running it against an up-to-date
database returns an empty list and changes nothing.

**Requirement: every migration script must itself be safe to re-run.**
A script is handed to ``sqlite3.Connection.executescript``, which commits any
pending transaction before it starts and opens no transaction of its own
unless the script does. So the script and the ``schema_migration`` row that
records it are *not* one atomic unit: a script that fails part-way leaves the
statements before the failure applied but the migration unrecorded, and the
next run will replay it from the top. Re-runnable statements
(``CREATE TABLE IF NOT EXISTS``, ``CREATE INDEX IF NOT EXISTS``, DML guarded
by ``WHERE NOT EXISTS`` / ``INSERT OR IGNORE``) turn that into a second
attempt; statements that are not re-runnable turn it into a manual repair.
The same requirement is stated in ``migrations/README.md``, where the next
person to add a ``.sql`` file will actually read it.

Only the tables the M1 collector actually writes exist so far (``crawl_run``
and ``fact``). ``claim`` / ``verdict`` / ``metric`` from docs/design.md
section 4 arrive as later migrations, when the code that writes them does.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

BOOKKEEPING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


def _open(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection, creating the parent directory when needed."""
    path = Path(db_path)
    if path.parent and str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connection(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open ``db_path`` for the duration of the block, then close it.

    A ``sqlite3.Connection`` is itself a context manager, but ``with conn:``
    scopes a *transaction*, not the connection: it commits or rolls back and
    leaves the connection -- and its file handle -- open. Handing out a bare
    connection therefore invites ``with <opener>(...) as conn:``, which reads
    like resource management and silently leaks the handle until the garbage
    collector happens to run. That is why ``_open`` is private.

    So this wrapper, rather than a bare opener, is how the package hands out a
    connection: both scopes end together. The block commits on success and
    rolls back on failure, and the connection is closed either way.

    The leak this replaces was not academic on Windows, where a still-open
    handle makes the database file undeletable (``WinError 32``) -- a test
    using ``tmp_path`` then fails in cleanup rather than where the bug is.
    """
    conn = _open(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


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
    """Apply every pending migration. Returns the versions applied, in order.

    A migration is applied and recorded before the next one is attempted. The
    script and its ``schema_migration`` row are not a single transaction --
    ``executescript`` will not allow that -- which is why every script has to
    be re-runnable (see the module docstring and ``migrations/README.md``).
    """
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
    """Open ``db_path``, bring it up to date, then close it.

    Returns the versions applied.
    """
    with connection(db_path) as conn:
        return apply_migrations(conn)
