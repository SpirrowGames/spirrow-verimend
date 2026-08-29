-- Verimend initial schema.
-- Columns follow docs/design.md section 4. Only the two tables the M1
-- collector writes are created here; claim / verdict / metric arrive with the
-- code that populates them (M2 / M3).

CREATE TABLE IF NOT EXISTS crawl_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    pr_url      TEXT,
    thread_id   TEXT,
    stats_json  TEXT
);

CREATE TABLE IF NOT EXISTS fact (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES crawl_run (id) ON DELETE CASCADE,
    source_kind  TEXT NOT NULL CHECK (source_kind IN ('file', 'tool_schema', 'service_health', 'config')),
    source_ref   TEXT NOT NULL,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fact_run_id ON fact (run_id);
CREATE INDEX IF NOT EXISTS idx_fact_content_hash ON fact (content_hash);
