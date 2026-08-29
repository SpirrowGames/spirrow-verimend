# Migrations

Forward-only. One file per migration, named `NNNN_short_name.sql`, applied in
filename order by `verimend.db.migrate` and recorded in the `schema_migration`
table. There are no down migrations.

## Rule: a migration script must be safe to re-run

The runner hands each script to `sqlite3.Connection.executescript`. That call
commits any pending transaction before it starts and opens no transaction of
its own unless the script does, so **the script and the `schema_migration` row
recording it are not one atomic unit**. If a script fails half-way, the
statements before the failure stay applied while the migration stays
unrecorded — and the next `verimend migrate` replays the whole file.

Write every statement so that replay is a no-op:

- `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`
- `INSERT OR IGNORE`, or `INSERT ... WHERE NOT EXISTS`, for seed data
- avoid bare `ALTER TABLE ... ADD COLUMN` (it has no `IF NOT EXISTS`); gate it
  in Python, or express the change as a new table plus a copy

`tests/test_migrations.py::test_migration_scripts_are_rerunnable` enforces this
by executing every script twice against the same database.

## Adding one

1. Add `NNNN_name.sql` here (next free number).
2. Extend `tests/test_migrations.py` with what the new schema must guarantee.
3. `uv run verimend migrate` applies it; running it again must report 0.
