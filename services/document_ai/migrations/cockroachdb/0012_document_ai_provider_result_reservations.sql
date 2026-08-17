-- Milestone 36 — durable provider-result reservations and reconciliation.

ALTER TABLE document_ai_provider_results
    ADD COLUMN IF NOT EXISTS provider_request_id TEXT NULL,
    ADD COLUMN IF NOT EXISTS request_fingerprint TEXT NULL,
    ADD COLUMN IF NOT EXISTS provider_result_reservation_id UUID NULL;

CREATE TABLE IF NOT EXISTS document_ai_provider_result_reservations (
    reservation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    processing_operation_id UUID NOT NULL,
    processing_attempt_id UUID NOT NULL,
    processing_work_item_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    source_artifact_id UUID NOT NULL,
    provider_name TEXT NOT NULL,
    model_policy TEXT NOT NULL,
    processing_policy_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    canonical_schema_version TEXT NOT NULL,
    source_scope_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    structural_scope_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_checksum_sha256 TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL,
    reservation_state TEXT NOT NULL DEFAULT 'reserved',
    reservation_generation INTEGER NOT NULL DEFAULT 1,
    reservation_expires_at TIMESTAMPTZ NULL,
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    in_progress_at TIMESTAMPTZ NULL,
    completed_at TIMESTAMPTZ NULL,
    provider_request_id TEXT NULL,
    provider_response_id TEXT NULL,
    provider_result_id UUID NULL,
    validated_result JSONB NULL,
    usage JSONB NULL,
    latency_ms INTEGER NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_provider_result_reservations_scope
        UNIQUE (tenant_id, reservation_id),
    CONSTRAINT uq_document_ai_provider_result_reservations_operation
        UNIQUE (tenant_id, processing_operation_id),
    CONSTRAINT fk_document_ai_provider_result_reservations_operation_scope
        FOREIGN KEY (tenant_id, processing_operation_id)
        REFERENCES document_ai_processing_operations (tenant_id, processing_operation_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_provider_result_reservations_attempt_scope
        FOREIGN KEY (tenant_id, processing_attempt_id)
        REFERENCES document_ai_processing_attempts (tenant_id, processing_attempt_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_provider_result_reservations_work_scope
        FOREIGN KEY (tenant_id, processing_work_item_id)
        REFERENCES document_ai_processing_work_items (tenant_id, processing_work_item_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_provider_result_reservations_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_provider_result_reservations_artifact_scope
        FOREIGN KEY (tenant_id, source_artifact_id)
        REFERENCES document_ai_source_artifacts (tenant_id, source_artifact_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_provider_result_reservations_state CHECK (
        reservation_state IN ('reserved', 'in_progress', 'completed', 'failed_retryable', 'failed_terminal')
    ),
    CONSTRAINT chk_document_ai_provider_result_reservations_generation CHECK (
        reservation_generation > 0
    ),
    CONSTRAINT chk_document_ai_provider_result_reservations_latency CHECK (
        latency_ms IS NULL OR latency_ms >= 0
    ),
    CONSTRAINT chk_document_ai_provider_result_reservations_size CHECK (
        source_size_bytes > 0
    )
);

ALTER TABLE document_ai_provider_results
    ADD CONSTRAINT IF NOT EXISTS fk_document_ai_provider_results_reservation_scope
    FOREIGN KEY (tenant_id, provider_result_reservation_id)
    REFERENCES document_ai_provider_result_reservations (tenant_id, reservation_id)
    ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_document_ai_provider_result_reservations_operation
    ON document_ai_provider_result_reservations (
        tenant_id,
        processing_operation_id,
        reservation_state,
        reservation_generation DESC
    );

CREATE INDEX IF NOT EXISTS idx_document_ai_provider_result_reservations_reconciliation
    ON document_ai_provider_result_reservations (
        tenant_id,
        reservation_state,
        reservation_expires_at,
        updated_at DESC
    );

CREATE INDEX IF NOT EXISTS idx_document_ai_provider_results_operation
    ON document_ai_provider_results (tenant_id, processing_operation_id, created_at DESC);
