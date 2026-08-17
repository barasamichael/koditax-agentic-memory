-- Milestone 9 / Document Policy 21.54, 21.58, 21.63, 21.74 and 21.88.
-- General processing operations remain the durable authority; extraction jobs
-- continue as a separate compatibility path for their existing callers.
BEGIN;

ALTER TABLE document_ai_processing_operations
    ADD COLUMN IF NOT EXISTS cancellation_requested_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS cancellation_requested_by_user_id UUID NULL,
    ADD COLUMN IF NOT EXISTS result_reference TEXT NULL,
    ADD COLUMN IF NOT EXISTS failure_category TEXT NULL;

ALTER TABLE document_ai_processing_operations
    ADD CONSTRAINT chk_document_ai_processing_operations_failure_category
    CHECK (failure_category IS NULL OR failure_category IN (
        'validation', 'source_unavailable', 'transient_dependency',
        'permanent_dependency', 'cancelled', 'internal'
    ));

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_operations_document_state
    ON document_ai_processing_operations (tenant_id, state, requested_at DESC);

COMMIT;
