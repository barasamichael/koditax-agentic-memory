BEGIN;

ALTER TABLE orchestration_conversation_state_records
    ADD COLUMN IF NOT EXISTS retention_expires_at TIMESTAMPTZ
    NOT NULL DEFAULT (now() + INTERVAL '90 days');

CREATE INDEX IF NOT EXISTS idx_orchestration_conversation_state_retention_expiry
    ON orchestration_conversation_state_records (retention_expires_at);

COMMIT;
