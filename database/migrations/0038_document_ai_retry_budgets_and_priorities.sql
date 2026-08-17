-- Milestone 12 / Document Policy 21.53-21.58 and 21.71-21.74.
-- Processing retries are durable work state, not queue redelivery state.
BEGIN;

ALTER TABLE document_ai_processing_work_items
    ADD COLUMN IF NOT EXISTS workload_class TEXT NOT NULL DEFAULT 'background',
    ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS first_attempted_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS max_retry_elapsed_seconds INTEGER NOT NULL DEFAULT 900,
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS failure_category TEXT NULL,
    ADD COLUMN IF NOT EXISTS dead_lettered_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS dead_letter_reason TEXT NULL,
    ADD COLUMN IF NOT EXISTS manual_recovery_count INTEGER NOT NULL DEFAULT 0;

UPDATE document_ai_processing_work_items
SET workload_class = 'background', priority = 10
WHERE workload_class = 'background' AND priority = 0;

ALTER TABLE document_ai_processing_work_items
    DROP CONSTRAINT IF EXISTS chk_document_ai_processing_work_items_state,
    ADD CONSTRAINT chk_document_ai_processing_work_items_state CHECK (
        state IN ('queued', 'leased', 'succeeded', 'failed', 'cancelled', 'dead_letter')
    ),
    ADD CONSTRAINT chk_document_ai_processing_work_items_workload_class CHECK (
        workload_class IN ('interactive', 'near_interactive', 'background', 'maintenance')
    ),
    ADD CONSTRAINT chk_document_ai_processing_work_items_priority CHECK (
        (workload_class = 'interactive' AND priority = 100) OR
        (workload_class = 'near_interactive' AND priority = 75) OR
        (workload_class = 'background' AND priority = 10) OR
        (workload_class = 'maintenance' AND priority = 1)
    ),
    ADD CONSTRAINT chk_document_ai_processing_work_items_retry_budget CHECK (
        retry_count >= 0 AND max_attempts > 0 AND retry_count < max_attempts
        AND max_retry_elapsed_seconds > 0 AND manual_recovery_count >= 0
    ),
    ADD CONSTRAINT chk_document_ai_processing_work_items_dead_letter CHECK (
        (state = 'dead_letter') = (dead_lettered_at IS NOT NULL)
    );

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_work_items_due_priority
    ON document_ai_processing_work_items
       (state, available_at, priority DESC, created_at)
    WHERE state = 'queued';

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_work_items_tenant_due
    ON document_ai_processing_work_items (tenant_id, state, available_at)
    WHERE state = 'queued';

COMMIT;
