-- Milestone 15 — General OpenAI understanding retains one result authority
-- per governed processing operation, even when a provider call is retried.
BEGIN;

ALTER TABLE document_ai_provider_results
    ADD COLUMN IF NOT EXISTS source_artifact_id UUID NULL,
    ADD COLUMN IF NOT EXISTS processing_work_item_id UUID NULL;

UPDATE document_ai_provider_results AS result
   SET source_artifact_id = artifact.source_artifact_id
  FROM document_ai_source_artifacts AS artifact
 WHERE artifact.tenant_id = result.tenant_id
   AND artifact.document_version_id = result.document_version_id
   AND result.source_artifact_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_provider_results_operation
    ON document_ai_provider_results (tenant_id, processing_operation_id);

ALTER TABLE document_ai_provider_results
    ADD CONSTRAINT fk_document_ai_provider_results_artifact_scope
    FOREIGN KEY (tenant_id, source_artifact_id)
    REFERENCES document_ai_source_artifacts (tenant_id, source_artifact_id)
    ON DELETE RESTRICT;

COMMIT;
