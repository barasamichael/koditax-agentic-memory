CREATE TABLE IF NOT EXISTS validation_executions (
    validation_id UUID PRIMARY KEY,
    return_id TEXT NOT NULL,
    tax_domain TEXT NOT NULL,
    validation_mode TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    issues_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    rule_results_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    correlation_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT validation_executions_tax_domain_check
        CHECK (tax_domain IN ('income_tax')),
    CONSTRAINT validation_executions_mode_check
        CHECK (validation_mode IN ('draft', 'pre_submission', 'post_submission_integrity')),
    CONSTRAINT validation_executions_status_check
        CHECK (validation_status IN ('accepted', 'rejected'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_validation_executions_request_fingerprint
    ON validation_executions (request_fingerprint);

CREATE INDEX IF NOT EXISTS idx_validation_executions_return_id
    ON validation_executions (return_id);

CREATE INDEX IF NOT EXISTS idx_validation_executions_created_at
    ON validation_executions (created_at DESC);
