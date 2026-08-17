-- Milestone 17 — canonical candidates are validated before guarded activation.
BEGIN;

ALTER TABLE document_ai_canonical_representations
    ADD COLUMN IF NOT EXISTS canonical_validation_version TEXT NULL,
    ADD COLUMN IF NOT EXISTS validation_report JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS readiness_state TEXT NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS validated_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_document_ai_canonical_representations_readiness_state'
    ) THEN
        ALTER TABLE document_ai_canonical_representations
            ADD CONSTRAINT chk_document_ai_canonical_representations_readiness_state
            CHECK (readiness_state IN ('none', 'partial', 'full'));
    END IF;
END $$;

-- uq_document_ai_active_canonical_representation already serializes active authority.
CREATE INDEX IF NOT EXISTS idx_document_ai_canonical_validation_readiness
    ON document_ai_canonical_representations
       (tenant_id, document_version_id, readiness_state, created_at DESC);

COMMIT;
