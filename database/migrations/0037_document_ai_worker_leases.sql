-- Milestone 11 / Document Policy 9.10-9.15 and 9.34-9.35.
-- The processing work item remains the sole current-claim authority.  Attempts
-- are immutable history apart from their own terminal outcome.
BEGIN;

ALTER TABLE document_ai_processing_work_items
    ADD COLUMN IF NOT EXISTS current_processing_attempt_id UUID NULL,
    ADD COLUMN IF NOT EXISTS fencing_token BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS lease_issued_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ NULL;

ALTER TABLE document_ai_processing_work_items
    ADD CONSTRAINT fk_document_ai_processing_work_items_current_attempt_scope
    FOREIGN KEY (tenant_id, current_processing_attempt_id)
    REFERENCES document_ai_processing_attempts (tenant_id, processing_attempt_id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE document_ai_processing_attempts
    ADD COLUMN IF NOT EXISTS worker_id TEXT NULL,
    ADD COLUMN IF NOT EXISTS fencing_token BIGINT NULL,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
    ADD CONSTRAINT chk_document_ai_processing_attempts_checkpoint_sequence
    CHECK (checkpoint_sequence >= 0);

ALTER TABLE document_ai_processing_checkpoints
    ADD COLUMN IF NOT EXISTS sequence INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD CONSTRAINT chk_document_ai_processing_checkpoints_sequence CHECK (sequence > 0);

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_work_items_lease_recovery
    ON document_ai_processing_work_items (state, leased_until)
    WHERE state = 'leased';

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_attempts_current_lease
    ON document_ai_processing_attempts (tenant_id, processing_work_item_id, lease_expires_at);

COMMIT;
