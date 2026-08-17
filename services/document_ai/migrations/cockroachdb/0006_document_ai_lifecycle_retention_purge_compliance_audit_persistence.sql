-- Document AI CockroachDB lifecycle, retention, purge, compliance, and audit persistence.
--
-- This migration ports the durable lifecycle coordination tables and the
-- compliance/audit persistence used by the current Document AI runtime.  It
-- stays additive and CockroachDB-compatible while preserving existing
-- lifecycle semantics and JSON payload compatibility.

CREATE TABLE IF NOT EXISTS document_ai_compliance_overrides (
    override_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    requested_action TEXT NOT NULL,
    requested_by_user_id UUID NOT NULL,
    requested_by_role TEXT NOT NULL,
    justification TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    approved_by_user_id UUID NULL,
    approved_by_role TEXT NULL,
    approved_at TIMESTAMPTZ NULL,
    rejected_by_user_id UUID NULL,
    rejected_by_role TEXT NULL,
    rejected_at TIMESTAMPTZ NULL,
    consumed_by_user_id UUID NULL,
    consumed_at TIMESTAMPTZ NULL,
    response_payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    CONSTRAINT chk_document_ai_compliance_overrides_status CHECK (
        status IN ('requested', 'approved', 'rejected', 'expired', 'consumed')
    )
);

CREATE INDEX IF NOT EXISTS idx_document_ai_compliance_overrides_document_id
    ON document_ai_compliance_overrides (document_id, created_at DESC);

CREATE TABLE IF NOT EXISTS document_ai_lifecycle_audit_evidence (
    audit_evidence_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    action TEXT NOT NULL,
    action_status TEXT NOT NULL,
    previous_state TEXT NULL,
    new_state TEXT NULL,
    user_id UUID NOT NULL,
    reason_code TEXT NULL,
    trace_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_document_ai_lifecycle_audit_document_id
    ON document_ai_lifecycle_audit_evidence (document_id, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_document_ai_lifecycle_audit_correlation_id
    ON document_ai_lifecycle_audit_evidence (correlation_id);

CREATE TABLE IF NOT EXISTS document_ai_compliance_override_audit_evidence (
    audit_evidence_id TEXT PRIMARY KEY,
    override_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    event_status TEXT NOT NULL,
    requested_action TEXT NOT NULL,
    actor_user_id UUID NOT NULL,
    actor_role TEXT NOT NULL,
    reason_code TEXT NULL,
    state_before TEXT NULL,
    state_after TEXT NULL,
    trace_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_document_ai_compliance_override_audit_document_id
    ON document_ai_compliance_override_audit_evidence (document_id, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_document_ai_compliance_override_audit_override_id
    ON document_ai_compliance_override_audit_evidence (override_id);

CREATE INDEX IF NOT EXISTS idx_document_ai_compliance_override_audit_correlation_id
    ON document_ai_compliance_override_audit_evidence (correlation_id);

CREATE TABLE IF NOT EXISTS document_ai_purge_operations (
    purge_operation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    document_version_id UUID NULL,
    state TEXT NOT NULL DEFAULT 'requested',
    requested_by_user_id UUID NOT NULL,
    requested_by_role TEXT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    correlation_id TEXT NOT NULL,
    idempotency_key TEXT NULL,
    request_fingerprint TEXT NULL,
    payload_fingerprint TEXT NULL,
    manifest_version TEXT NOT NULL DEFAULT 'v1',
    replay_count INTEGER NOT NULL DEFAULT 0,
    last_reconciled_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_purge_operations_scope UNIQUE (tenant_id, purge_operation_id),
    CONSTRAINT fk_document_ai_purge_operations_document_scope
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES document_ai_documents (tenant_id, document_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_purge_operations_state CHECK (
        state IN ('requested', 'running', 'completed', 'failed', 'cancelled')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_purge_operations_idempotency
    ON document_ai_purge_operations (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_document_ai_purge_operations_scope
    ON document_ai_purge_operations (tenant_id, document_id, requested_at DESC);

CREATE TABLE IF NOT EXISTS document_ai_purge_targets (
    purge_target_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    purge_operation_id UUID NOT NULL,
    target_kind TEXT NOT NULL,
    target_reference TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    completed_at TIMESTAMPTZ NULL,
    failure_detail JSONB NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    verified_at TIMESTAMPTZ NULL,
    required BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_purge_targets_scope UNIQUE (tenant_id, purge_target_id),
    CONSTRAINT fk_document_ai_purge_targets_operation_scope
        FOREIGN KEY (tenant_id, purge_operation_id)
        REFERENCES document_ai_purge_operations (tenant_id, purge_operation_id) ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_purge_targets_reference
        UNIQUE (tenant_id, purge_operation_id, target_kind, target_reference),
    CONSTRAINT chk_document_ai_purge_targets_state CHECK (
        state IN ('pending', 'running', 'completed', 'failed', 'skipped')
    )
);

CREATE INDEX IF NOT EXISTS idx_document_ai_purge_unresolved
    ON document_ai_purge_targets (tenant_id, purge_operation_id, state)
    WHERE required;

CREATE TABLE IF NOT EXISTS document_ai_purge_attempts (
    purge_attempt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    purge_operation_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'running',
    requested_by_user_id UUID NULL,
    requested_by_role TEXT NULL,
    correlation_id TEXT NOT NULL,
    request_fingerprint TEXT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    failure_detail JSONB NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_purge_attempts_scope UNIQUE (tenant_id, purge_attempt_id),
    CONSTRAINT uq_document_ai_purge_attempts_number
        UNIQUE (tenant_id, purge_operation_id, attempt_number),
    CONSTRAINT fk_document_ai_purge_attempts_operation_scope
        FOREIGN KEY (tenant_id, purge_operation_id)
        REFERENCES document_ai_purge_operations (tenant_id, purge_operation_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_purge_attempts_number CHECK (attempt_number > 0),
    CONSTRAINT chk_document_ai_purge_attempts_state CHECK (
        state IN ('running', 'succeeded', 'failed', 'cancelled')
    )
);

CREATE INDEX IF NOT EXISTS idx_document_ai_purge_attempts_operation_scope
    ON document_ai_purge_attempts (tenant_id, purge_operation_id, attempt_number DESC);
