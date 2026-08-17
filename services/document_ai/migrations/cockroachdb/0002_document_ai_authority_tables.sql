-- Document AI CockroachDB authority tables.
--
-- This migration materializes the durable authority boundary for upload
-- sessions, document registration, version lineage, source-artifact lineage,
-- document bindings, and signed-access usage state in CockroachDB-compatible
-- DDL.

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
    compliance_lock_until TIMESTAMPTZ NULL,
    display_name TEXT NULL,
    category TEXT NULL,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    description TEXT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    registry_revision INTEGER NOT NULL DEFAULT 0,
    active_document_version_id UUID NULL
);

ALTER TABLE document_ai_documents
    ADD CONSTRAINT IF NOT EXISTS chk_document_ai_documents_registry_revision
    CHECK (registry_revision >= 0);

ALTER TABLE document_ai_documents
    ADD CONSTRAINT IF NOT EXISTS chk_document_ai_documents_revision_nonnegative
    CHECK (revision >= 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_documents_tenant_document
    ON document_ai_documents (tenant_id, document_id);

CREATE INDEX IF NOT EXISTS idx_document_ai_documents_scope
    ON document_ai_documents (tenant_id, owner_user_id, uploaded_at DESC, document_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_document_ai_documents_scope_checksum
    ON document_ai_documents (tenant_id, owner_user_id, checksum_sha256);

CREATE INDEX IF NOT EXISTS idx_document_ai_documents_visible_scope
    ON document_ai_documents (tenant_id, owner_user_id, state, uploaded_at, document_id);

CREATE INDEX IF NOT EXISTS idx_document_ai_documents_exact_metadata
    ON document_ai_documents (tenant_id, owner_user_id, state, active_document_version_id, document_id);

CREATE TABLE IF NOT EXISTS document_ai_completion_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    response_payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS document_ai_signed_access_usage (
    capability_id TEXT PRIMARY KEY,
    consumed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_ai_document_versions (
    document_version_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    version_number INTEGER NOT NULL,
    version_state TEXT NOT NULL DEFAULT 'current',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    supersedes_document_version_id UUID NULL,
    idempotency_key TEXT NULL
);

ALTER TABLE document_ai_document_versions
    ADD CONSTRAINT IF NOT EXISTS fk_document_ai_document_versions_document_scope
    FOREIGN KEY (tenant_id, document_id)
    REFERENCES document_ai_documents (tenant_id, document_id) ON DELETE RESTRICT;

ALTER TABLE document_ai_document_versions
    ADD CONSTRAINT IF NOT EXISTS uq_document_ai_document_versions_scope
    UNIQUE (tenant_id, document_version_id);

ALTER TABLE document_ai_document_versions
    ADD CONSTRAINT IF NOT EXISTS fk_document_ai_document_versions_supersedes
    FOREIGN KEY (supersedes_document_version_id)
    REFERENCES document_ai_document_versions (document_version_id) ON DELETE RESTRICT;

ALTER TABLE document_ai_document_versions
    ADD CONSTRAINT IF NOT EXISTS uq_document_ai_document_versions_number
    UNIQUE (tenant_id, document_id, version_number);

ALTER TABLE document_ai_document_versions
    ADD CONSTRAINT IF NOT EXISTS chk_document_ai_document_versions_number
    CHECK (version_number > 0);

ALTER TABLE document_ai_document_versions
    ADD CONSTRAINT IF NOT EXISTS chk_document_ai_document_versions_state
    CHECK (version_state IN ('current', 'superseded', 'retired', 'purged'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_document_versions_idempotency
    ON document_ai_document_versions (tenant_id, document_id, idempotency_key);

CREATE UNIQUE INDEX IF NOT EXISTS idx_document_ai_document_versions_scope
    ON document_ai_document_versions (tenant_id, document_id, version_number DESC);

CREATE TABLE IF NOT EXISTS document_ai_source_artifacts (
    source_artifact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_version_id UUID NOT NULL,
    storage_key TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    retention_state TEXT NOT NULL DEFAULT 'active',
    integrity_state TEXT NOT NULL DEFAULT 'verified',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum_algorithm TEXT NULL,
    verified_media_type TEXT NULL
);

ALTER TABLE document_ai_source_artifacts
    ADD CONSTRAINT IF NOT EXISTS fk_document_ai_source_artifacts_version_scope
    FOREIGN KEY (tenant_id, document_version_id)
    REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT;

ALTER TABLE document_ai_source_artifacts
    ADD CONSTRAINT IF NOT EXISTS uq_document_ai_source_artifacts_storage_key
    UNIQUE (tenant_id, storage_key);

ALTER TABLE document_ai_source_artifacts
    ADD CONSTRAINT IF NOT EXISTS chk_document_ai_source_artifacts_checksum
    CHECK (checksum_sha256 ~ '^[a-f0-9]{64}$');

ALTER TABLE document_ai_source_artifacts
    ADD CONSTRAINT IF NOT EXISTS chk_document_ai_source_artifacts_size
    CHECK (size_bytes > 0);

ALTER TABLE document_ai_source_artifacts
    ADD CONSTRAINT IF NOT EXISTS chk_document_ai_source_artifacts_retention_state
    CHECK (retention_state IN ('active', 'held', 'purge_pending', 'purged'));

ALTER TABLE document_ai_source_artifacts
    ADD CONSTRAINT IF NOT EXISTS chk_document_ai_source_artifacts_integrity_state
    CHECK (integrity_state IN ('pending', 'verified', 'mismatch', 'unavailable'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_source_artifacts_scope
    ON document_ai_source_artifacts (tenant_id, source_artifact_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_source_artifacts_version
    ON document_ai_source_artifacts (tenant_id, document_version_id);

CREATE INDEX IF NOT EXISTS idx_document_ai_source_artifacts_version
    ON document_ai_source_artifacts (tenant_id, document_version_id);

ALTER TABLE document_ai_documents
    ADD CONSTRAINT IF NOT EXISTS fk_document_ai_documents_active_version_scope
    FOREIGN KEY (tenant_id, active_document_version_id)
    REFERENCES document_ai_document_versions (tenant_id, document_version_id)
    ON DELETE RESTRICT;

CREATE TABLE IF NOT EXISTS document_ai_document_bindings (
    document_binding_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    document_version_id UUID NULL,
    binding_kind TEXT NOT NULL,
    binding_scope TEXT NOT NULL,
    bound_by_user_id UUID NULL,
    bound_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    binding_role TEXT NULL,
    conversation_id TEXT NULL,
    turn_id TEXT NULL,
    workflow_id TEXT NULL,
    attachment_order INTEGER NULL,
    correlation_id TEXT NULL,
    revoked_at TIMESTAMPTZ NULL
);

ALTER TABLE document_ai_document_bindings
    ADD CONSTRAINT IF NOT EXISTS fk_document_ai_document_bindings_document_scope
    FOREIGN KEY (tenant_id, document_id)
    REFERENCES document_ai_documents (tenant_id, document_id) ON DELETE RESTRICT;

ALTER TABLE document_ai_document_bindings
    ADD CONSTRAINT IF NOT EXISTS fk_document_ai_document_bindings_version_scope
    FOREIGN KEY (tenant_id, document_version_id)
    REFERENCES document_ai_document_versions (tenant_id, document_version_id)
    ON DELETE RESTRICT;

ALTER TABLE document_ai_document_bindings
    ADD CONSTRAINT IF NOT EXISTS chk_document_ai_document_bindings_expiry
    CHECK (expires_at IS NULL OR expires_at > bound_at);

ALTER TABLE document_ai_document_bindings
    ADD CONSTRAINT IF NOT EXISTS chk_document_ai_document_bindings_role
    CHECK (
        binding_role IS NULL OR binding_role IN (
            'conversation_attachment',
            'current_turn_attachment',
            'existing_library_document',
            'workflow_reference'
        )
    );

ALTER TABLE document_ai_document_bindings
    ADD CONSTRAINT IF NOT EXISTS chk_document_ai_document_bindings_target
    CHECK (
        (conversation_id IS NOT NULL AND workflow_id IS NULL)
        OR (conversation_id IS NULL AND workflow_id IS NOT NULL)
        OR (conversation_id IS NULL AND workflow_id IS NULL)
    );

ALTER TABLE document_ai_document_bindings
    ADD CONSTRAINT IF NOT EXISTS chk_document_ai_document_bindings_turn
    CHECK (turn_id IS NULL OR conversation_id IS NOT NULL);

ALTER TABLE document_ai_document_bindings
    ADD CONSTRAINT IF NOT EXISTS chk_document_ai_document_bindings_attachment_order
    CHECK (attachment_order IS NULL OR attachment_order >= 0);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_document_bindings_scope_id
    ON document_ai_document_bindings (tenant_id, document_binding_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_document_bindings_logical_target
    ON document_ai_document_bindings (
        tenant_id,
        document_id,
        COALESCE(document_version_id, '00000000-0000-0000-0000-000000000000'::uuid),
        binding_role,
        COALESCE(conversation_id, ''),
        COALESCE(turn_id, ''),
        COALESCE(workflow_id, '')
    )
    WHERE revoked_at IS NULL AND binding_role IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_document_ai_document_bindings_scope
    ON document_ai_document_bindings (tenant_id, document_id, binding_kind, bound_at DESC);

CREATE INDEX IF NOT EXISTS idx_document_ai_document_bindings_conversation
    ON document_ai_document_bindings (tenant_id, conversation_id, turn_id, attachment_order, bound_at)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_document_ai_document_bindings_workflow
    ON document_ai_document_bindings (tenant_id, workflow_id, bound_at)
    WHERE revoked_at IS NULL;
