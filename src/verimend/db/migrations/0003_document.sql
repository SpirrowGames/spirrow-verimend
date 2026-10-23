-- Per-document extraction record, the key of incremental extraction
-- (docs/design.md 4 and 5.2, M2b extractor).
--
-- A document of a later run whose (doc_repo, doc_path, content_hash,
-- extractor_key, chunk_hash) equals a row here with status 'extracted' or
-- 'cached', in a run that is not 'failed', copies that run's claims instead of
-- calling the LLM.
--
-- content_hash   SHA-256 of the document text
-- extractor_key  SHA-256 of the prompt, the tier and the output schema version
-- chunk_hash     SHA-256 of the computed chunk boundaries
-- status         extracted | cached | failed (any chunk failed or was aborted;
--                never a cache source)

CREATE TABLE IF NOT EXISTS document (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES crawl_run (id) ON DELETE CASCADE,
    doc_repo      TEXT NOT NULL,
    doc_path      TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    extractor_key TEXT NOT NULL,
    chunk_hash    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('extracted', 'cached', 'failed')),
    UNIQUE (run_id, doc_repo, doc_path)
);

CREATE INDEX IF NOT EXISTS idx_document_cache_key
    ON document (doc_repo, doc_path, content_hash, extractor_key, chunk_hash);
