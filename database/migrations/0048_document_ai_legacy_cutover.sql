-- Milestone 27: durable, resumable lineage for migration from extraction records.
BEGIN;

CREATE TABLE IF NOT EXISTS document_ai_legacy_migrations (
    tenant_id TEXT NOT NULL,
    legacy_document_id UUID NOT NULL,
    target_document_id UUID NULL,
    target_document_version_id UUID NULL,
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    checkpoint TEXT NOT NULL DEFAULT 'discovered',
    exception_code TEXT NULL,
    exception_detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, legacy_document_id),
    CONSTRAINT fk_document_ai_legacy_migrations_document
      FOREIGN KEY (tenant_id, legacy_document_id)
      REFERENCES document_ai_documents (tenant_id, document_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_legacy_migrations_state
      CHECK (state IN ('pending', 'running', 'migrated', 'blocked', 'rolled_back'))
);

CREATE TABLE IF NOT EXISTS document_ai_legacy_migration_observations (
    tenant_id TEXT NOT NULL,
    legacy_extraction_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    canonical_element_id UUID NOT NULL,
    field_name TEXT NOT NULL,
    observed_value JSONB NOT NULL,
    observation_state TEXT NOT NULL DEFAULT 'historical',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, legacy_extraction_id, field_name),
    CONSTRAINT fk_document_ai_legacy_migration_observation_version
      FOREIGN KEY (tenant_id, document_version_id)
      REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_legacy_migration_observation_element
      FOREIGN KEY (tenant_id, canonical_element_id)
      REFERENCES document_ai_canonical_elements (tenant_id, canonical_element_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_legacy_migration_observation_state
      CHECK (observation_state = 'historical')
);

CREATE TABLE IF NOT EXISTS document_ai_legacy_compatibility_callers (
    caller_id TEXT PRIMARY KEY,
    approved_until TIMESTAMPTZ NOT NULL,
    removal_case TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_ai_legacy_compatibility_traffic (
    compatibility_traffic_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    caller_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    operation_name TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_document_ai_legacy_compatibility_caller
      FOREIGN KEY (caller_id) REFERENCES document_ai_legacy_compatibility_callers (caller_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_document_ai_legacy_migrations_reconcile
  ON document_ai_legacy_migrations (state, updated_at);
CREATE INDEX IF NOT EXISTS idx_document_ai_legacy_compatibility_traffic_recent
  ON document_ai_legacy_compatibility_traffic (occurred_at DESC);

COMMIT;
