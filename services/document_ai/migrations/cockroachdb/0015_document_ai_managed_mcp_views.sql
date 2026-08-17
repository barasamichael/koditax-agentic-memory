-- Document AI CockroachDB sanitized managed-MCP inspection views.
--
-- These views intentionally expose only read-only operational summaries for
-- external inspection surfaces. They exclude raw bytes, storage keys, signed
-- URLs, provider payloads, tokens, and mutation-capable structures.

CREATE OR REPLACE VIEW document_ai_mcp_document_summary AS
SELECT
    document.tenant_id,
    document.document_id,
    document.owner_user_id,
    document.state,
    document.uploaded_at,
    document.display_name,
    document.category,
    document.revision,
    document.registry_revision,
    document.active_document_version_id,
    version.version_number AS active_version_number,
    version.version_state AS active_version_state,
    document.purge_eligible_at,
    document.purged_at,
    document.compliance_lock_until
FROM document_ai_documents AS document
LEFT JOIN document_ai_document_versions AS version
    ON version.tenant_id = document.tenant_id
   AND version.document_version_id = document.active_document_version_id;

CREATE OR REPLACE VIEW document_ai_mcp_processing_status AS
SELECT
    operation.tenant_id,
    operation.processing_operation_id,
    operation.document_version_id,
    version.document_id,
    operation.operation_kind,
    operation.state AS operation_state,
    operation.requested_at,
    operation.completed_at,
    operation.failure_category,
    operation.cancellation_requested_at,
    work.processing_work_item_id,
    work.work_kind,
    work.state AS work_state,
    work.priority,
    work.retry_count,
    work.max_attempts,
    work.next_retry_at,
    work.leased_until,
    work.dead_lettered_at
FROM document_ai_processing_operations AS operation
LEFT JOIN document_ai_processing_work_items AS work
    ON work.tenant_id = operation.tenant_id
   AND work.processing_operation_id = operation.processing_operation_id
LEFT JOIN document_ai_document_versions AS version
    ON version.tenant_id = operation.tenant_id
   AND version.document_version_id = operation.document_version_id;

CREATE OR REPLACE VIEW document_ai_mcp_evidence_lineage AS
SELECT
    item.tenant_id,
    item.evidence_item_id,
    item.document_version_id,
    item.semantic_meaning,
    item.derivation_type,
    item.assurance_state,
    item.completeness_state,
    item.correction_state,
    item.conflict_state,
    item.created_at AS evidence_created_at,
    requirement.evidence_requirement_id,
    requirement.requirement_source,
    requirement.expected_value_type,
    requirement.multiplicity,
    source.evidence_source_id,
    source.canonical_element_id,
    source.source_region_id,
    source.source_artifact_id
FROM document_ai_evidence_items AS item
LEFT JOIN document_ai_evidence_requirements AS requirement
    ON requirement.tenant_id = item.tenant_id
   AND requirement.evidence_requirement_id = item.evidence_requirement_id
LEFT JOIN document_ai_evidence_sources AS source
    ON source.tenant_id = item.tenant_id
   AND source.evidence_item_id = item.evidence_item_id;

CREATE OR REPLACE VIEW document_ai_mcp_correction_status AS
SELECT
    correction.tenant_id,
    correction.correction_id,
    correction.document_version_id,
    correction.canonical_element_id,
    correction.evidence_item_id,
    correction.correction_state,
    correction.policy_version,
    correction.supersedes_correction_id,
    correction.reversal_of_correction_id,
    correction.created_at,
    correction.updated_at,
    effective.active_correction_id,
    effective.correction_state AS effective_correction_state,
    remapping.state AS remapping_state
FROM document_ai_corrections AS correction
LEFT JOIN document_ai_effective_values AS effective
    ON effective.tenant_id = correction.tenant_id
   AND effective.canonical_element_id = correction.canonical_element_id
LEFT JOIN document_ai_correction_remappings AS remapping
    ON remapping.tenant_id = correction.tenant_id
   AND remapping.correction_id = correction.correction_id;

CREATE OR REPLACE VIEW document_ai_mcp_unresolved_evidence_conflicts AS
SELECT
    conflict.tenant_id,
    conflict.evidence_conflict_id,
    conflict.evidence_item_id,
    conflict.conflicting_evidence_item_id,
    conflict.state,
    conflict.resolved_at,
    conflict.created_at,
    conflict.updated_at
FROM document_ai_evidence_conflicts AS conflict
WHERE conflict.state IN ('open', 'resolving');
