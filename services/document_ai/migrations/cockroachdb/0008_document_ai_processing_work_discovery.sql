-- Milestone 25 / CockroachDB-backed work discovery.
--
-- The bounded candidate query scans queued work by availability and priority
-- without mutating lease state.  This index matches that deterministic order.
CREATE INDEX IF NOT EXISTS idx_document_ai_processing_work_items_discovery
    ON document_ai_processing_work_items (
        available_at,
        priority DESC,
        created_at,
        processing_work_item_id
    )
    STORING (
        tenant_id,
        processing_operation_id,
        work_kind,
        state,
        leased_until,
        current_processing_attempt_id,
        retry_count,
        max_attempts,
        next_retry_at,
        failure_category,
        dead_lettered_at
    )
    WHERE state = 'queued';
