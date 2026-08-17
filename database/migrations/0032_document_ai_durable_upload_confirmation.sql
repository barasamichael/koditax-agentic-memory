-- Milestone 6 / FR-001, FR-002, FR-003, FR-018, FR-019.
-- Accepted document state and its asynchronous processing request are coupled
-- transactionally.  Publishing is deliberately deferred to an outbox relay.
BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_processing_operations_ingestion
    ON document_ai_processing_operations (tenant_id, document_version_id, operation_kind);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_processing_work_items_operation_kind
    ON document_ai_processing_work_items (tenant_id, processing_operation_id, work_kind);

CREATE TABLE IF NOT EXISTS document_ai_processing_outbox (
    processing_outbox_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    processing_operation_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT NULL,
    published_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_processing_outbox_scope
        UNIQUE (tenant_id, processing_outbox_id),
    CONSTRAINT uq_document_ai_processing_outbox_operation_event
        UNIQUE (tenant_id, processing_operation_id, event_type),
    CONSTRAINT fk_document_ai_processing_outbox_operation_scope
        FOREIGN KEY (tenant_id, processing_operation_id)
        REFERENCES document_ai_processing_operations (tenant_id, processing_operation_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_processing_outbox_state CHECK (
        state IN ('pending', 'publishing', 'published', 'failed')
    ),
    CONSTRAINT chk_document_ai_processing_outbox_publish_attempts CHECK (
        publish_attempts >= 0
    )
);

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_outbox_pending
    ON document_ai_processing_outbox (state, created_at)
    WHERE state IN ('pending', 'failed');

COMMIT;
