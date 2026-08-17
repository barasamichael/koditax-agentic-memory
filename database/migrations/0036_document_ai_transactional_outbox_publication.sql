-- Milestone 10 / Document Policy 10.41 and 10.42.
-- The existing processing outbox remains the sole durable publication authority.
BEGIN;

ALTER TABLE document_ai_processing_outbox
    ADD COLUMN IF NOT EXISTS processing_work_item_id UUID NULL,
    ADD COLUMN IF NOT EXISTS routing_key TEXT NOT NULL DEFAULT 'document_ai.processing',
    ADD COLUMN IF NOT EXISTS correlation_id TEXT NULL,
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS claim_token UUID NULL,
    ADD COLUMN IF NOT EXISTS last_error_class TEXT NULL;

UPDATE document_ai_processing_outbox AS outbox
SET processing_work_item_id = work_item.processing_work_item_id
FROM document_ai_processing_work_items AS work_item
WHERE outbox.tenant_id = work_item.tenant_id
  AND outbox.processing_operation_id = work_item.processing_operation_id
  AND work_item.work_kind = 'general_document_understanding'
  AND outbox.processing_work_item_id IS NULL;

ALTER TABLE document_ai_processing_outbox
    ALTER COLUMN processing_work_item_id SET NOT NULL;

ALTER TABLE document_ai_processing_outbox
    ADD CONSTRAINT fk_document_ai_processing_outbox_work_item_scope
    FOREIGN KEY (tenant_id, processing_work_item_id)
    REFERENCES document_ai_processing_work_items (tenant_id, processing_work_item_id)
    ON DELETE RESTRICT;

ALTER TABLE document_ai_processing_outbox
    ADD CONSTRAINT chk_document_ai_processing_outbox_routing_key
    CHECK (routing_key = 'document_ai.processing'),
    ADD CONSTRAINT chk_document_ai_processing_outbox_error_class
    CHECK (last_error_class IS NULL OR last_error_class IN ('transient', 'permanent'));

CREATE TABLE IF NOT EXISTS document_ai_processing_outbox_attempts (
    processing_outbox_attempt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    processing_outbox_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    claim_token UUID NOT NULL,
    state TEXT NOT NULL,
    error_code TEXT NULL,
    error_class TEXT NULL,
    broker_message_id TEXT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at TIMESTAMPTZ NULL,
    CONSTRAINT uq_document_ai_processing_outbox_attempt_scope
        UNIQUE (tenant_id, processing_outbox_attempt_id),
    CONSTRAINT uq_document_ai_processing_outbox_attempt_number
        UNIQUE (tenant_id, processing_outbox_id, attempt_number),
    CONSTRAINT fk_document_ai_processing_outbox_attempt_outbox_scope
        FOREIGN KEY (tenant_id, processing_outbox_id)
        REFERENCES document_ai_processing_outbox (tenant_id, processing_outbox_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_processing_outbox_attempt_number CHECK (attempt_number > 0),
    CONSTRAINT chk_document_ai_processing_outbox_attempt_state CHECK (
        state IN ('attempted', 'acknowledged', 'transient_failure', 'permanent_failure')
    ),
    CONSTRAINT chk_document_ai_processing_outbox_attempt_error_class CHECK (
        error_class IS NULL OR error_class IN ('transient', 'permanent')
    )
);

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_outbox_reconciliation
    ON document_ai_processing_outbox (next_attempt_at, created_at)
    WHERE state IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_outbox_stale_claim
    ON document_ai_processing_outbox (claimed_at)
    WHERE state = 'publishing';

COMMIT;
