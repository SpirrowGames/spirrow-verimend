-- Lexora decisions.db schema, copied verbatim from SpirrowGames/spirrow-lexora
-- 3e19e9052c0a29ecc94115afdcc3264fa8555586:src/lexora/decide/log.py (_CREATE_TABLE_SQL) by `git show` (T06 DoD 8').
-- Do not edit by hand: re-copy from a pinned Lexora commit and update the sha above.
CREATE TABLE IF NOT EXISTS decisions (
    decision_id       TEXT PRIMARY KEY,
    policy            TEXT NOT NULL,
    state_hash        TEXT NOT NULL,
    questions_hash    TEXT NOT NULL,
    questions_version TEXT NULL,
    provider          TEXT NOT NULL,
    answers_json      TEXT NOT NULL,
    latency_ms        INTEGER NOT NULL,
    timestamp         TEXT NOT NULL,
    provider_error    TEXT NULL,
    provider_model    TEXT NULL,
    provider_input_tokens  INTEGER NULL,
    provider_output_tokens INTEGER NULL,
    shadow_of         TEXT NULL
)
