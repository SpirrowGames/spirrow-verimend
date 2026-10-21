-- Claims and their verdicts (docs/design.md section 4, M2).
--
-- claim.subject_json is the structured subject the extractor states a claim is
-- about (verimend/claims.py). It is what the deterministic checkers compare;
-- they never read claim.text. NULL means the extractor gave none.
--
-- verdict.claim_id is UNIQUE: a claim has exactly one verdict, and a re-run of
-- the reconciler cannot add a second.
--
-- The per-document content_hash for incremental crawls (section 4) is not here:
-- it arrives with the extractor that writes it.

CREATE TABLE IF NOT EXISTS claim (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES crawl_run (id) ON DELETE CASCADE,
    doc_repo         TEXT NOT NULL,
    doc_path         TEXT NOT NULL,
    anchor_start     INTEGER,
    anchor_end       INTEGER,
    text             TEXT NOT NULL,
    claim_type       TEXT NOT NULL CHECK (claim_type IN (
                         'link', 'version', 'command', 'parameter',
                         'port', 'path', 'behavior', 'architecture')),
    extraction_model TEXT,
    subject_json     TEXT
);

CREATE INDEX IF NOT EXISTS idx_claim_run_id ON claim (run_id);

CREATE TABLE IF NOT EXISTS verdict (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id      INTEGER NOT NULL UNIQUE REFERENCES claim (id) ON DELETE CASCADE,
    result        TEXT NOT NULL CHECK (result IN ('verified', 'stale', 'unverifiable')),
    method        TEXT NOT NULL CHECK (method IN ('deterministic', 'llm')),
    evidence_json TEXT NOT NULL,
    patch_draft   TEXT
);
