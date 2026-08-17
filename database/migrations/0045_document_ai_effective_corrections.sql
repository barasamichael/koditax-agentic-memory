-- Milestone 23: append-only correction events and targeted dependency invalidation.
BEGIN;

ALTER TABLE document_ai_corrections
    ADD COLUMN IF NOT EXISTS correction_state TEXT NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS reversal_of_correction_id UUID NULL,
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT NULL,
    ADD COLUMN IF NOT EXISTS source_observed_value JSONB NULL,
    ADD COLUMN IF NOT EXISTS original_interpreted_value JSONB NULL,
    ADD COLUMN IF NOT EXISTS effective_value JSONB NULL,
    ADD COLUMN IF NOT EXISTS policy_version TEXT NOT NULL DEFAULT 'v1';

ALTER TABLE document_ai_corrections
    ADD CONSTRAINT chk_document_ai_corrections_state
    CHECK (correction_state IN ('active', 'reversed', 'superseded')),
    ADD CONSTRAINT fk_document_ai_corrections_reversal
    FOREIGN KEY (reversal_of_correction_id)
    REFERENCES document_ai_corrections (correction_id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_corrections_idempotency
    ON document_ai_corrections (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS document_ai_effective_values (
    tenant_id TEXT NOT NULL,
    canonical_element_id UUID NOT NULL,
    source_observed_value JSONB NOT NULL,
    original_interpreted_value JSONB NOT NULL,
    corrected_value JSONB NULL,
    effective_value JSONB NOT NULL,
    active_correction_id UUID NULL,
    correction_state TEXT NOT NULL DEFAULT 'original',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, canonical_element_id),
    CONSTRAINT fk_document_ai_effective_values_element
      FOREIGN KEY (tenant_id, canonical_element_id)
      REFERENCES document_ai_canonical_elements (tenant_id, canonical_element_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_effective_values_correction
      FOREIGN KEY (active_correction_id)
      REFERENCES document_ai_corrections (correction_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_effective_values_state
      CHECK (correction_state IN ('original', 'corrected'))
);

CREATE TABLE IF NOT EXISTS document_ai_correction_invalidations (
    correction_invalidation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    correction_id UUID NOT NULL,
    dependency_kind TEXT NOT NULL,
    dependency_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    UNIQUE (tenant_id, correction_id, dependency_kind, dependency_id),
    FOREIGN KEY (tenant_id, correction_id)
      REFERENCES document_ai_corrections (tenant_id, correction_id) ON DELETE RESTRICT,
    CHECK (dependency_kind IN ('effective_canonical', 'retrieval_chunk', 'embedding', 'evidence', 'workflow_projection', 'cache')),
    CHECK (state IN ('pending', 'completed', 'failed'))
);

-- Current values are derived only from immutable source and correction records;
-- direct mutation is prohibited outside the correction transaction.
CREATE OR REPLACE FUNCTION fn_document_ai_effective_values_prevent_source_rewrite()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source_observed_value IS DISTINCT FROM OLD.source_observed_value
       OR NEW.original_interpreted_value IS DISTINCT FROM OLD.original_interpreted_value THEN
        RAISE EXCEPTION 'document_ai effective values cannot rewrite observations';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_document_ai_effective_values_prevent_source_rewrite ON document_ai_effective_values;
CREATE TRIGGER trg_document_ai_effective_values_prevent_source_rewrite
BEFORE UPDATE ON document_ai_effective_values
FOR EACH ROW EXECUTE FUNCTION fn_document_ai_effective_values_prevent_source_rewrite();

COMMIT;
