CREATE TABLE IF NOT EXISTS document_ai_upload_sessions (
    session_id UUID PRIMARY KEY,
    document_id UUID NOT NULL,
    session_state TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    tenant_id TEXT NOT NULL,
    owner_user_id UUID NOT NULL,
    content_type TEXT NOT NULL,
    expected_size_bytes BIGINT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    request_fingerprint TEXT NOT NULL,
    request_payload JSONB NOT NULL,
    response_payload JSONB NOT NULL,
    session_record JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_ai_upload_sessions_document_id
    ON document_ai_upload_sessions (document_id);

CREATE INDEX IF NOT EXISTS idx_document_ai_upload_sessions_scope
    ON document_ai_upload_sessions (tenant_id, owner_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS document_ai_documents (
    document_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id UUID NOT NULL,
    state TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    content_type TEXT NOT NULL,
    computation_id TEXT NULL,
    purge_eligible_at TIMESTAMPTZ NULL,
    purged_at TIMESTAMPTZ NULL,
    compliance_lock_until TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_document_ai_documents_scope
    ON document_ai_documents (tenant_id, owner_user_id, uploaded_at DESC, document_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_document_ai_documents_scope_checksum
    ON document_ai_documents (tenant_id, owner_user_id, checksum_sha256);

CREATE TABLE IF NOT EXISTS document_ai_completion_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    response_payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS document_ai_extraction_jobs (
    extraction_job_id UUID PRIMARY KEY,
    extraction_id UUID UNIQUE NOT NULL,
    document_id UUID NOT NULL,
    owner_user_id UUID NOT NULL,
    extraction_profile TEXT NOT NULL,
    provider_name TEXT NULL,
    source_reference TEXT NULL,
    queued_at TIMESTAMPTZ NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    request_fingerprint TEXT NOT NULL,
    response_payload JSONB NOT NULL,
    queued_job_payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_ai_extraction_jobs_document_id
    ON document_ai_extraction_jobs (document_id, queued_at DESC);

CREATE TABLE IF NOT EXISTS document_ai_extractions (
    extraction_id UUID PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    persisted_payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_ai_extractions_document_id
    ON document_ai_extractions (((persisted_payload ->> 'document_id')));

CREATE TABLE IF NOT EXISTS document_ai_extraction_verifications (
    idempotency_key TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    response_payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS document_ai_evidence_linkages (
    linkage_id TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    persisted_payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_ai_evidence_linkages_document_id
    ON document_ai_evidence_linkages (((persisted_payload ->> 'document_id')));

CREATE TABLE IF NOT EXISTS document_ai_signed_access_usage (
    capability_id TEXT PRIMARY KEY,
    consumed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS document_ai_compliance_overrides (
    override_id TEXT PRIMARY KEY,
    response_payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_ai_compliance_overrides_document_id
    ON document_ai_compliance_overrides (((response_payload -> 'override' ->> 'document_id')));

CREATE TABLE IF NOT EXISTS document_ai_dead_letters (
    dead_letter_id TEXT PRIMARY KEY,
    extraction_job_id UUID NOT NULL,
    document_id UUID NOT NULL,
    failure_class TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    trace_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    audit_evidence_id TEXT NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_ai_dead_letters_document_id
    ON document_ai_dead_letters (document_id, created_at DESC);

CREATE TABLE IF NOT EXISTS document_ai_lifecycle_audit_evidence (
    audit_evidence_id TEXT PRIMARY KEY,
    document_id UUID NOT NULL,
    correlation_id TEXT NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_ai_lifecycle_audit_document_id
    ON document_ai_lifecycle_audit_evidence (document_id, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_document_ai_lifecycle_audit_correlation_id
    ON document_ai_lifecycle_audit_evidence (correlation_id);

CREATE TABLE IF NOT EXISTS document_ai_compliance_override_audit_evidence (
    audit_evidence_id TEXT PRIMARY KEY,
    override_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    correlation_id TEXT NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_ai_compliance_override_audit_document_id
    ON document_ai_compliance_override_audit_evidence (document_id, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_document_ai_compliance_override_audit_override_id
    ON document_ai_compliance_override_audit_evidence (override_id);

CREATE INDEX IF NOT EXISTS idx_document_ai_compliance_override_audit_correlation_id
    ON document_ai_compliance_override_audit_evidence (correlation_id);
