"""Migrations must apply to an empty database and be idempotent."""

import sqlite3
from pathlib import Path

import pytest

from verimend.db import apply_migrations, connection, migrate, pending_migrations
from verimend.db.migrate import MIGRATIONS_DIR

EXPECTED_TABLES = {"crawl_run", "fact", "schema_migration"}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in rows}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_applies_to_an_empty_database(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "verimend.sqlite3"
    applied = migrate(db)

    assert applied == ["0001_crawl_run_and_fact"]
    assert db.exists()

    with connection(db) as conn:
        assert EXPECTED_TABLES <= _tables(conn)
        assert _columns(conn, "crawl_run") == [
            "id",
            "started_at",
            "finished_at",
            "status",
            "pr_url",
            "thread_id",
            "stats_json",
        ]
        assert _columns(conn, "fact") == [
            "id",
            "run_id",
            "source_kind",
            "source_ref",
            "content",
            "content_hash",
        ]


def test_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "verimend.sqlite3"
    assert migrate(db) == ["0001_crawl_run_and_fact"]

    with connection(db) as conn:
        assert pending_migrations(conn) == []
        assert apply_migrations(conn) == []
        assert apply_migrations(conn) == []
        recorded = [row["version"] for row in conn.execute("SELECT version FROM schema_migration")]

    assert recorded == ["0001_crawl_run_and_fact"]


def test_idempotent_run_preserves_data(tmp_path: Path) -> None:
    db = tmp_path / "verimend.sqlite3"
    migrate(db)

    with connection(db) as conn:
        conn.execute("INSERT INTO crawl_run (started_at, status) VALUES ('2026-08-29T00:00:00Z', 'running')")
        conn.commit()

    migrate(db)

    with connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM crawl_run").fetchone()["n"] == 1


def test_fact_rejects_unknown_source_kind(tmp_path: Path) -> None:
    """docs/design.md section 4 enumerates source_kind; the schema enforces it."""
    db = tmp_path / "verimend.sqlite3"
    migrate(db)

    with connection(db) as conn:
        run_id = conn.execute(
            "INSERT INTO crawl_run (started_at, status) VALUES ('2026-08-29T00:00:00Z', 'running')"
        ).lastrowid
        conn.execute(
            "INSERT INTO fact (run_id, source_kind, source_ref, content, content_hash)"
            " VALUES (?, 'tool_schema', 'magickit/tools.py', '{}', 'abc')",
            (run_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fact (run_id, source_kind, source_ref, content, content_hash)"
                " VALUES (?, 'screenshot', 'x', 'y', 'z')",
                (run_id,),
            )


def test_fact_requires_an_existing_run(tmp_path: Path) -> None:
    db = tmp_path / "verimend.sqlite3"
    migrate(db)

    with connection(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fact (run_id, source_kind, source_ref, content, content_hash)"
                " VALUES (999, 'file', 'README.md', 'text', 'hash')"
            )


def test_migration_scripts_are_rerunnable(tmp_path: Path) -> None:
    """Every script must survive a replay -- see migrations/README.md.

    ``executescript`` cannot wrap a script and its ``schema_migration`` row in
    one transaction, so a script that fails half-way is replayed in full by the
    next run. Re-runnability is what makes that recoverable, so it is a
    requirement of the format rather than a property of today's single file.
    """
    scripts = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert scripts, "no migration scripts found"

    db = tmp_path / "verimend.sqlite3"
    with connection(db) as conn:
        for script in scripts:
            sql = script.read_text(encoding="utf-8")
            conn.executescript(sql)
            before = _tables(conn)
            changed = _replay_changes_row_counts(conn, sql)  # replay: must not raise
            assert _tables(conn) == before
            assert changed == {}, f"{script.name}: a replay changed row counts (table: (before, after)): {changed}"


def _user_table_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row count of every user table, enumerated from ``sqlite_master``.

    Table names are deliberately not hard-coded: the next table a migration
    adds is covered without anyone having to remember to list it here.

    ``NOT LIKE 'sqlite_%'`` leaves out SQLite's internal tables. The one that
    matters is ``sqlite_sequence``, a by-product of AUTOINCREMENT: a replay
    that inserts into a user table is already caught on that table itself.

    ``schema_migration`` needs no special case. It is created by
    ``BOOKKEEPING_DDL`` in ``migrate.py``, not by any script, so on this raw
    ``executescript`` path it does not exist and is not enumerated. Should a
    script ever create it, it is enumerated -- and covered -- automatically.
    """
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in names}


def _replay_changes_row_counts(conn: sqlite3.Connection, sql: str) -> dict[str, tuple[int | None, int | None]]:
    """Replay ``sql`` and return the tables whose row count differs, as (before, after).

    An empty result means the replay left every user table's row count alone.
    Today's ``0001`` has no DML, so every count is 0 both times and this is
    trivially empty; the check exists to arm itself the moment a script
    gains DML that is not guarded against a replay.
    """
    before = _user_table_row_counts(conn)
    conn.executescript(sql)
    after = _user_table_row_counts(conn)
    return {
        name: (before.get(name), after.get(name))
        for name in sorted(before.keys() | after.keys())
        if before.get(name) != after.get(name)
    }


def test_row_count_trap_fires_on_an_unguarded_insert(tmp_path: Path) -> None:
    """The trap above must actually trip, or it is indistinguishable from an always-green test.

    The table is synthetic, created by this test, so naming it here does not
    contradict the no-hard-coding rule for the real migrations.
    """
    sql = "CREATE TABLE IF NOT EXISTS t (x); INSERT INTO t VALUES (1);"
    with connection(tmp_path / "trap.sqlite3") as conn:
        conn.executescript(sql)
        assert _replay_changes_row_counts(conn, sql) == {"t": (1, 2)}


def test_row_count_trap_stays_quiet_on_a_guarded_insert(tmp_path: Path) -> None:
    sql = "CREATE TABLE IF NOT EXISTS t (x PRIMARY KEY); INSERT OR IGNORE INTO t VALUES (1);"
    with connection(tmp_path / "trap.sqlite3") as conn:
        conn.executescript(sql)
        assert _replay_changes_row_counts(conn, sql) == {}


def test_downstream_tables_are_not_created_yet(tmp_path: Path) -> None:
    """claim / verdict / metric arrive with the code that writes them (M2 / M3)."""
    db = tmp_path / "verimend.sqlite3"
    migrate(db)

    with connection(db) as conn:
        assert _tables(conn).isdisjoint({"claim", "verdict", "metric"})


def test_connection_is_closed_when_the_block_exits(tmp_path: Path) -> None:
    """``with connection(...)`` ends the connection, not just its transaction.

    ``sqlite3.Connection.__exit__`` commits or rolls back and leaves the
    connection open, so a bare opener under ``with`` leaks the handle. This is
    the assertion that the wrapper closes it.
    """
    db = tmp_path / "verimend.sqlite3"
    with connection(db) as conn:
        conn.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_connection_is_closed_when_the_block_raises(tmp_path: Path) -> None:
    """The failure path has to close too, or an error turns into a leak."""
    db = tmp_path / "verimend.sqlite3"
    escaped: sqlite3.Connection | None = None

    with pytest.raises(ZeroDivisionError):
        with connection(db) as conn:
            escaped = conn
            raise ZeroDivisionError("boom")

    assert escaped is not None
    with pytest.raises(sqlite3.ProgrammingError):
        escaped.execute("SELECT 1")


def test_migrate_closes_the_connection_it_opened(tmp_path: Path, monkeypatch) -> None:
    """``migrate()`` must not hand its connection to the garbage collector.

    A leaked handle is invisible on Linux until file descriptors run out, but
    on Windows it makes the database file undeletable (``WinError 32``), so the
    ``db.unlink()`` below is the regression check that actually bites.
    """
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", spy)

    db = tmp_path / "verimend.sqlite3"
    assert migrate(db) == ["0001_crawl_run_and_fact"]

    assert opened, "migrate() opened no connection"
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    db.unlink()
