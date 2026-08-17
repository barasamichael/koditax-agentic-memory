-- Document AI CockroachDB processing persistence.
--
-- This migration materializes the durable processing authority used by the
-- current Document AI worker and repository code.  It intentionally avoids
-- PostgreSQL-only trigger/function machinery so the lane stays CockroachDB-
-- compatible while preserving the existing processing model.

CREATE TABLE IF NOT EXISTS document_ai_processing_operations (
    processing_operation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_version_id UUID NOT NULL,
    operation_kind TEXT NOT NULL,
    processing_policy_version TEXT NOT NULL,
    processor_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    correlation_id TEXT NOT NULL,
    idempotency_key TEXT NULL,
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    cancellation_requested_at TIMESTAMPTZ NULL,
    cancellation_requested_by_user_id UUID NULL,
    result_reference TEXT NULL,
    failure_category TEXT NULL,
    CONSTRAINT uq_document_ai_processing_operations_scope
        UNIQUE (tenant_id, processing_operation_id),
    CONSTRAINT fk_document_ai_processing_operations_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_processing_operations_ingestion
        UNIQUE (tenant_id, document_version_id, operation_kind),
    CONSTRAINT uq_document_ai_processing_operations_idempotency
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT chk_document_ai_processing_operations_state CHECK (
        state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    CONSTRAINT chk_document_ai_processing_operations_completion CHECK (
        (state IN ('succeeded', 'failed', 'cancelled') AND completed_at IS NOT NULL)
        OR (state IN ('queued', 'running') AND completed_at IS NULL)
    ),
    CONSTRAINT chk_document_ai_processing_operations_failure_category CHECK (
        failure_category IS NULL OR length(failure_category) > 0
    )
);

CREATE TABLE IF NOT EXISTS document_ai_processing_work_items (
    processing_work_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    processing_operation_id UUID NOT NULL,
    work_kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    leased_until TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    current_processing_attempt_id UUID NULL,
    fencing_token BIGINT NOT NULL DEFAULT 0,
    lease_issued_at TIMESTAMPTZ NULL,
    last_heartbeat_at TIMESTAMPTZ NULL,
    workload_class TEXT NOT NULL DEFAULT 'background',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    first_attempted_at TIMESTAMPTZ NULL,
    max_retry_elapsed_seconds INTEGER NOT NULL DEFAULT 900,
    next_retry_at TIMESTAMPTZ NULL,
    failure_category TEXT NULL,
    dead_lettered_at TIMESTAMPTZ NULL,
    dead_letter_reason TEXT NULL,
    manual_recovery_count INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT uq_document_ai_processing_work_items_scope
        UNIQUE (tenant_id, processing_work_item_id),
    CONSTRAINT fk_document_ai_processing_work_items_operation_scope
        FOREIGN KEY (tenant_id, processing_operation_id)
        REFERENCES document_ai_processing_operations (tenant_id, processing_operation_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_processing_work_items_operation_kind
        UNIQUE (tenant_id, processing_operation_id, work_kind),
    CONSTRAINT chk_document_ai_processing_work_items_state CHECK (
        state IN ('queued', 'leased', 'succeeded', 'failed', 'cancelled', 'dead_letter')
    ),
    CONSTRAINT chk_document_ai_processing_work_items_workload_class CHECK (
        workload_class IN ('interactive', 'near_interactive', 'background', 'maintenance')
    ),
    CONSTRAINT chk_document_ai_processing_work_items_priority CHECK (
        (workload_class = 'interactive' AND priority = 100)
        OR (workload_class = 'near_interactive' AND priority = 75)
        OR (workload_class = 'background' AND priority = 10)
        OR (workload_class = 'maintenance' AND priority = 1)
    ),
    CONSTRAINT chk_document_ai_processing_work_items_retry_budget CHECK (
        retry_count >= 0
        AND max_attempts > 0
        AND retry_count < max_attempts
        AND max_retry_elapsed_seconds > 0
        AND manual_recovery_count >= 0
    ),
    CONSTRAINT chk_document_ai_processing_work_items_dead_letter CHECK (
        (state = 'dead_letter') = (dead_lettered_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_operations_scope
    ON document_ai_processing_operations (tenant_id, document_version_id, requested_at DESC);

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_operations_document_state
    ON document_ai_processing_operations (tenant_id, state, requested_at DESC);

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_work_items_claim
    ON document_ai_processing_work_items (state, available_at, priority DESC);

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_work_items_lease_recovery
    ON document_ai_processing_work_items (state, leased_until)
    WHERE state = 'leased';

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_work_items_due_priority
    ON document_ai_processing_work_items
        (state, available_at, priority DESC, created_at)
    WHERE state = 'queued';

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_work_items_tenant_due
    ON document_ai_processing_work_items (tenant_id, state, available_at)
    WHERE state = 'queued';

CREATE TABLE IF NOT EXISTS document_ai_processing_attempts (
    processing_attempt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    processing_work_item_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ NULL,
    error_code TEXT NULL,
    error_detail JSONB NULL,
    worker_id TEXT NULL,
    fencing_token BIGINT NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    last_heartbeat_at TIMESTAMPTZ NULL,
    checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT uq_document_ai_processing_attempts_scope
        UNIQUE (tenant_id, processing_attempt_id),
    CONSTRAINT fk_document_ai_processing_attempts_work_item_scope
        FOREIGN KEY (tenant_id, processing_work_item_id)
        REFERENCES document_ai_processing_work_items (tenant_id, processing_work_item_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_processing_attempts_number
        UNIQUE (tenant_id, processing_work_item_id, attempt_number),
    CONSTRAINT chk_document_ai_processing_attempts_number CHECK (attempt_number > 0),
    CONSTRAINT chk_document_ai_processing_attempts_state CHECK (
        state IN ('running', 'succeeded', 'failed', 'cancelled')
    ),
    CONSTRAINT chk_document_ai_processing_attempts_checkpoint_sequence CHECK (
        checkpoint_sequence >= 0
    )
);

ALTER TABLE document_ai_processing_work_items
    ADD CONSTRAINT IF NOT EXISTS fk_document_ai_processing_work_items_current_attempt_scope
    FOREIGN KEY (tenant_id, current_processing_attempt_id)
    REFERENCES document_ai_processing_attempts (tenant_id, processing_attempt_id)
    ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_attempts_current_lease
    ON document_ai_processing_attempts (tenant_id, processing_work_item_id, lease_expires_at);

CREATE TABLE IF NOT EXISTS document_ai_processing_checkpoints (
    processing_checkpoint_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    processing_attempt_id UUID NOT NULL,
    checkpoint_key TEXT NOT NULL,
    checkpoint_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sequence INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_processing_checkpoints_scope
        UNIQUE (tenant_id, processing_checkpoint_id),
    CONSTRAINT fk_document_ai_processing_checkpoints_attempt_scope
        FOREIGN KEY (tenant_id, processing_attempt_id)
        REFERENCES document_ai_processing_attempts (tenant_id, processing_attempt_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_processing_checkpoints_key
        UNIQUE (tenant_id, processing_attempt_id, checkpoint_key),
    CONSTRAINT chk_document_ai_processing_checkpoints_sequence CHECK (sequence > 0)
);

CREATE TABLE IF NOT EXISTS document_ai_processing_outbox (
    processing_outbox_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    processing_operation_id UUID NOT NULL,
    processing_work_item_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT NULL,
    published_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    routing_key TEXT NOT NULL DEFAULT 'document_ai.processing',
    correlation_id TEXT NULL,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ NULL,
    claim_token UUID NULL,
    last_error_class TEXT NULL,
    CONSTRAINT uq_document_ai_processing_outbox_scope
        UNIQUE (tenant_id, processing_outbox_id),
    CONSTRAINT uq_document_ai_processing_outbox_operation_event
        UNIQUE (tenant_id, processing_operation_id, event_type),
    CONSTRAINT fk_document_ai_processing_outbox_operation_scope
        FOREIGN KEY (tenant_id, processing_operation_id)
        REFERENCES document_ai_processing_operations (tenant_id, processing_operation_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_processing_outbox_work_item_scope
        FOREIGN KEY (tenant_id, processing_work_item_id)
        REFERENCES document_ai_processing_work_items (tenant_id, processing_work_item_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_processing_outbox_state CHECK (
        state IN ('pending', 'publishing', 'published', 'failed')
    ),
    CONSTRAINT chk_document_ai_processing_outbox_publish_attempts CHECK (
        publish_attempts >= 0
    ),
    CONSTRAINT chk_document_ai_processing_outbox_routing_key CHECK (
        routing_key = 'document_ai.processing'
    ),
    CONSTRAINT chk_document_ai_processing_outbox_error_class CHECK (
        last_error_class IS NULL OR last_error_class IN ('transient', 'permanent')
    )
);

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

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_outbox_pending
    ON document_ai_processing_outbox (state, created_at)
    WHERE state IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_outbox_reconciliation
    ON document_ai_processing_outbox (next_attempt_at, created_at)
    WHERE state IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS idx_document_ai_processing_outbox_stale_claim
    ON document_ai_processing_outbox (claimed_at)
    WHERE state = 'publishing';

CREATE TABLE IF NOT EXISTS document_ai_provider_results (
    provider_result_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    processing_operation_id UUID NOT NULL,
    processing_attempt_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    source_artifact_id UUID NOT NULL,
    processing_work_item_id UUID NOT NULL,
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
    CONSTRAINT uq_document_ai_provider_results_scope
        UNIQUE (tenant_id, provider_result_id),
    CONSTRAINT uq_document_ai_provider_results_operation_attempt
        UNIQUE (tenant_id, processing_operation_id, processing_attempt_id),
    CONSTRAINT uq_document_ai_provider_results_operation
        UNIQUE (tenant_id, processing_operation_id),
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
    CONSTRAINT fk_document_ai_provider_results_artifact_scope
        FOREIGN KEY (tenant_id, source_artifact_id)
        REFERENCES document_ai_source_artifacts (tenant_id, source_artifact_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_provider_results_provider CHECK (provider_name = 'openai'),
    CONSTRAINT chk_document_ai_provider_results_state CHECK (provider_result_state = 'validated'),
    CONSTRAINT chk_document_ai_provider_results_latency CHECK (latency_ms >= 0)
);

CREATE INDEX IF NOT EXISTS idx_document_ai_provider_results_operation
    ON document_ai_provider_results (tenant_id, processing_operation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS document_ai_source_inspections (
    source_inspection_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_version_id UUID NOT NULL,
    source_artifact_id UUID NOT NULL,
    processing_operation_id UUID NOT NULL,
    policy_version TEXT NOT NULL,
    disposition TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    observed_media_type TEXT NULL,
    page_count INTEGER NULL,
    structural_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    inspected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_source_inspections_scope
        UNIQUE (tenant_id, source_inspection_id),
    CONSTRAINT uq_document_ai_source_inspections_version_policy
        UNIQUE (tenant_id, document_version_id, policy_version),
    CONSTRAINT fk_document_ai_source_inspections_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_source_inspections_artifact_scope
        FOREIGN KEY (tenant_id, source_artifact_id)
        REFERENCES document_ai_source_artifacts (tenant_id, source_artifact_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_source_inspections_operation_scope
        FOREIGN KEY (tenant_id, processing_operation_id)
        REFERENCES document_ai_processing_operations (tenant_id, processing_operation_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_source_inspections_disposition
        CHECK (disposition IN ('accepted', 'quarantined')),
    CONSTRAINT chk_document_ai_source_inspections_reason_code CHECK (
        reason_code IN (
            'accepted',
            'source_empty',
            'source_too_large',
            'unsupported_format',
            'declared_media_type_mismatch',
            'malformed_document',
            'encrypted_document',
            'unsafe_active_content',
            'archive_not_permitted',
            'invalid_office_container',
            'image_dimensions_too_large',
            'structured_text_too_deep',
            'malformed_pdf',
            'encrypted_pdf',
            'active_content'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_document_ai_source_inspections_gate
    ON document_ai_source_inspections (tenant_id, document_version_id, policy_version, disposition);
