BEGIN;

CREATE INDEX IF NOT EXISTS idx_computation_results_user_id
    ON computation_results (user_id);

CREATE INDEX IF NOT EXISTS idx_document_extractions_user_id
    ON document_extractions (user_id);

CREATE INDEX IF NOT EXISTS idx_forms_document_id
    ON forms (document_id);

CREATE INDEX IF NOT EXISTS idx_reports_form_id
    ON reports (form_id);

CREATE INDEX IF NOT EXISTS idx_submissions_form_id
    ON submissions (form_id);

CREATE INDEX IF NOT EXISTS idx_submissions_report_id
    ON submissions (report_id);

CREATE INDEX IF NOT EXISTS idx_validations_user_id
    ON validations (user_id);

CREATE INDEX IF NOT EXISTS idx_audit_events_chain_lookup
    ON audit_events (
        user_id,
        resource_type,
        resource_id,
        event_timestamp DESC,
        created_at DESC,
        id DESC
    );

CREATE INDEX IF NOT EXISTS idx_documents_computation_state_purged
    ON documents (computation_id, state, purged_at);

COMMIT;
