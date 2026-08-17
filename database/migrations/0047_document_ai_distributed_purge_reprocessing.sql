-- Milestone 26 — durable distributed purge and candidate-only reprocessing.
BEGIN;

ALTER TABLE document_ai_purge_operations
    ADD COLUMN IF NOT EXISTS manifest_version TEXT NOT NULL DEFAULT 'v1',
    ADD COLUMN IF NOT EXISTS replay_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_reconciled_at TIMESTAMPTZ NULL;

ALTER TABLE document_ai_purge_targets
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS required BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS document_ai_reprocessing_candidates (
    reprocessing_candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_version_id UUID NOT NULL,
    processing_operation_id UUID NULL,
    prior_active_representation_id UUID NULL,
    candidate_representation_id UUID NULL,
    model_policy_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    canonical_schema_version TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'building',
    validation_report JSONB NOT NULL DEFAULT '{}'::jsonb,
    activated_at TIMESTAMPTZ NULL,
    rolled_back_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_reprocessing_candidates_scope UNIQUE (tenant_id, reprocessing_candidate_id),
    CONSTRAINT fk_document_ai_reprocessing_candidate_version
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_reprocessing_candidate_prior
        FOREIGN KEY (tenant_id, prior_active_representation_id)
        REFERENCES document_ai_canonical_representations (tenant_id, canonical_representation_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_reprocessing_candidate_representation
        FOREIGN KEY (tenant_id, candidate_representation_id)
        REFERENCES document_ai_canonical_representations (tenant_id, canonical_representation_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_reprocessing_candidate_state
        CHECK (state IN ('building', 'validated', 'rejected', 'active', 'rolled_back'))
);

CREATE TABLE IF NOT EXISTS document_ai_correction_remappings (
    correction_remapping_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    reprocessing_candidate_id UUID NOT NULL,
    correction_id UUID NOT NULL,
    prior_stable_key TEXT NOT NULL,
    candidate_stable_key TEXT NULL,
    state TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_document_ai_correction_remappings_candidate
        FOREIGN KEY (tenant_id, reprocessing_candidate_id)
        REFERENCES document_ai_reprocessing_candidates (tenant_id, reprocessing_candidate_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_correction_remappings_state
        CHECK (state IN ('preserved', 'remapped', 'unresolved')),
    CONSTRAINT uq_document_ai_correction_remappings_candidate_correction
        UNIQUE (tenant_id, reprocessing_candidate_id, correction_id)
);

CREATE INDEX IF NOT EXISTS idx_document_ai_purge_unresolved
    ON document_ai_purge_targets (tenant_id, purge_operation_id, state)
    WHERE required;
CREATE INDEX IF NOT EXISTS idx_document_ai_reprocessing_candidates_scope
    ON document_ai_reprocessing_candidates (tenant_id, document_version_id, state, created_at DESC);

-- Candidates must build their own chunks/vectors before atomic activation.  They
-- are not retrievable because all retrieval queries retain the active authority
-- predicate; only the candidate build trigger is relaxed.
CREATE OR REPLACE FUNCTION fn_document_ai_retrieval_chunk_active_authority()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM document_ai_canonical_representations representation
         WHERE representation.tenant_id = NEW.tenant_id
           AND representation.canonical_representation_id = NEW.canonical_representation_id
           AND representation.document_version_id = NEW.document_version_id
           AND ((representation.is_active AND representation.state = 'active')
             OR (representation.state = 'validated' AND NOT representation.is_active))
    ) THEN
        RAISE EXCEPTION 'retrieval chunks require active or validated candidate authority';
    END IF;
    RETURN NEW;
END $$;

COMMIT;
