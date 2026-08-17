-- Milestone 13 — Complete Safe Source Inspection.
BEGIN;

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
    CONSTRAINT uq_document_ai_source_inspections_scope UNIQUE (tenant_id, source_inspection_id),
    CONSTRAINT uq_document_ai_source_inspections_version_policy
        UNIQUE (tenant_id, document_version_id, policy_version),
    CONSTRAINT fk_document_ai_source_inspections_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_source_inspections_artifact_scope
        FOREIGN KEY (tenant_id, source_artifact_id)
        REFERENCES document_ai_source_artifacts (tenant_id, source_artifact_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_source_inspections_operation_scope
        FOREIGN KEY (tenant_id, processing_operation_id)
        REFERENCES document_ai_processing_operations (tenant_id, processing_operation_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_source_inspections_disposition
        CHECK (disposition IN ('accepted', 'quarantined'))
);

CREATE INDEX IF NOT EXISTS idx_document_ai_source_inspections_gate
    ON document_ai_source_inspections (tenant_id, document_version_id, policy_version, disposition);

-- Semantic work is eligible only after the current policy accepts this exact version.
CREATE OR REPLACE FUNCTION fn_document_ai_general_work_requires_inspection()
RETURNS TRIGGER AS $$
DECLARE operation_version UUID;
BEGIN
    IF NEW.work_kind = 'general_document_understanding' THEN
        SELECT document_version_id INTO operation_version
        FROM document_ai_processing_operations
        WHERE tenant_id = NEW.tenant_id
          AND processing_operation_id = NEW.processing_operation_id;
        IF NOT EXISTS (
            SELECT 1 FROM document_ai_source_inspections
            WHERE tenant_id = NEW.tenant_id AND document_version_id = operation_version
              AND policy_version = 'v1' AND disposition = 'accepted'
        ) THEN
            RAISE EXCEPTION 'source_inspection_required_before_general_processing';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_document_ai_general_work_requires_inspection
    ON document_ai_processing_work_items;
CREATE TRIGGER trg_document_ai_general_work_requires_inspection
    BEFORE INSERT ON document_ai_processing_work_items
    FOR EACH ROW EXECUTE FUNCTION fn_document_ai_general_work_requires_inspection();

COMMIT;
