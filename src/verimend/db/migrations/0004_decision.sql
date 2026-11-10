-- Lexora /v1/decide judgements around the mender (T06, verimend.decide).
--
-- One row per verdict and stage:
--   stage        drift (before the mender) | patch_risk (after it)
--   mode         the decide_mode in force when the judgement was made. publish
--                applies only 'enforce' rows; an 'observe' row is a record and
--                never changes what is published, whatever mode publishes later.
--   decision_id  Lexora's id for the judgement, the join key into Lexora's
--                decisions.db (the evaluation's source of truth). NULL when no
--                call was made or it returned no signal.
--   patch_sha256 SHA-256 of the patch_draft the patch_risk row judged. publish
--                applies the row only while it matches the current draft.
--   outcome      see the CHECK list.

CREATE TABLE IF NOT EXISTS decision (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id   INTEGER NOT NULL REFERENCES verdict (id) ON DELETE CASCADE,
    stage        TEXT NOT NULL CHECK (stage IN ('drift', 'patch_risk')),
    mode         TEXT NOT NULL CHECK (mode IN ('observe', 'enforce')),
    decision_id  TEXT,
    patch_sha256 TEXT,
    outcome      TEXT NOT NULL CHECK (outcome IN (
                     'no_signal', 'exempt', 'skip_drift', 'skip_mapping',
                     'generated_low_conf', 'generated', 'auto', 'review',
                     'review_no_patch_signal', 'review_mixed_group', 'findings_only')),
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decision_verdict ON decision (verdict_id, stage);
