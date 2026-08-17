-- Milestone 14 — Implement the Governed OpenAI Processing Boundary.
-- Provider output remains an internal, non-canonical artifact until later
-- canonical assembly validates and consumes it.
BEGIN;

CREATE TABLE IF NOT EXISTS document_ai_provider_results (
    provider_result_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    processing_operation_id UUID NOT NULL,
    processing_attempt_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    provider_name TEXT NOT NULL,
    provider_response_id TEXT NULL,
    model_policy TEXT NOT NULL,
    processing_policy_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    canonical_schema_version TEXT NOT NULL,
    source_scope_id TEXT NOT NULL,
    provider_result_state TEXT NOT NULL,
    validated_result JSONB NOT NULL,
    usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    latency_ms INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_provider_results_scope UNIQUE (tenant_id, provider_result_id),
    CONSTRAINT uq_document_ai_provider_results_operation_attempt
        UNIQUE (tenant_id, processing_operation_id, processing_attempt_id),
    CONSTRAINT fk_document_ai_provider_results_operation_scope
        FOREIGN KEY (tenant_id, processing_operation_id)
        REFERENCES document_ai_processing_operations (tenant_id, processing_operation_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_provider_results_attempt_scope
        FOREIGN KEY (tenant_id, processing_attempt_id)
        REFERENCES document_ai_processing_attempts (tenant_id, processing_attempt_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_provider_results_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_provider_results_provider CHECK (provider_name = 'openai'),
    CONSTRAINT chk_document_ai_provider_results_state CHECK (provider_result_state = 'validated'),
    CONSTRAINT chk_document_ai_provider_results_latency CHECK (latency_ms >= 0)
);

CREATE INDEX IF NOT EXISTS idx_document_ai_provider_results_operation
    ON document_ai_provider_results (tenant_id, processing_operation_id, created_at DESC);

COMMIT;
