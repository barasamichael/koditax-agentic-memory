-- Milestone 31 / CockroachDB-backed processing dead-letter authority.
--
-- Processing dead letters are durable terminal records, not queue redelivery
-- artifacts.  They preserve the exact work, operation, attempt, and failure
-- lineage that established terminality.
CREATE TABLE IF NOT EXISTS document_ai_processing_dead_letters (
    processing_dead_letter_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    processing_operation_id UUID NOT NULL,
    processing_work_item_id UUID NOT NULL,
    processing_attempt_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    document_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    source_artifact_id UUID NOT NULL,
    work_kind TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    fencing_token BIGINT NOT NULL,
    failure_class TEXT NOT NULL,
    failure_category TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    retry_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    max_retry_elapsed_seconds INTEGER NOT NULL,
    correlation_id TEXT NOT NULL,
    error_code TEXT NOT NULL,
    error_detail JSONB NOT NULL,
    dead_lettered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    diagnostic_payload JSONB NOT NULL,
    CONSTRAINT uq_document_ai_processing_dead_letters_attempt
        UNIQUE (tenant_id, processing_attempt_id),
    CONSTRAINT chk_document_ai_processing_dead_letters_failure_class CHECK (
        failure_class IN ('retry_exhausted', 'non_retryable_failure')
    ),
    CONSTRAINT chk_document_ai_processing_dead_letters_reason_code CHECK (
        length(reason_code) > 0
    ),
    CONSTRAINT chk_document_ai_processing_dead_letters_failure_category CHECK (
        length(failure_category) > 0
    ),
    CONSTRAINT chk_document_ai_processing_dead_letters_error_code CHECK (
        length(error_code) > 0
    )
);

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_dead_letters_document_id
    ON document_ai_processing_dead_letters (document_id, dead_lettered_at DESC);

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_dead_letters_operation_id
    ON document_ai_processing_dead_letters (tenant_id, processing_operation_id, dead_lettered_at DESC);

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_dead_letters_work_item_id
    ON document_ai_processing_dead_letters (tenant_id, processing_work_item_id, dead_lettered_at DESC);
