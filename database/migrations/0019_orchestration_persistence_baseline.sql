BEGIN;

CREATE TABLE IF NOT EXISTS orchestration_execution_records (
    execution_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    route_id TEXT,
    target_service TEXT,
    target_operation TEXT,
    plan_id TEXT NOT NULL,
    plan_version TEXT NOT NULL,
    plan_status TEXT NOT NULL,
    tenant_id TEXT,
    user_id TEXT,
    execution_status TEXT NOT NULL,
    envelope JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_orchestration_execution_records_execution_status CHECK (
        execution_status IN ('resolved', 'rejected')
    ),
    CONSTRAINT chk_orchestration_execution_records_idempotency_key_not_blank CHECK (
        char_length(btrim(idempotency_key)) > 0
    ),
    CONSTRAINT chk_orchestration_execution_records_request_fingerprint_not_blank CHECK (
        char_length(btrim(request_fingerprint)) > 0
    ),
    CONSTRAINT chk_orchestration_execution_records_plan_id_not_blank CHECK (
        char_length(btrim(plan_id)) > 0
    )
);

CREATE INDEX IF NOT EXISTS idx_orchestration_execution_records_correlation_id
    ON orchestration_execution_records (correlation_id, created_at, execution_id);

CREATE INDEX IF NOT EXISTS idx_orchestration_execution_records_request_fingerprint
    ON orchestration_execution_records (request_fingerprint);

CREATE TABLE IF NOT EXISTS orchestration_audit_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    trace_id TEXT,
    correlation_id TEXT,
    tenant_id TEXT,
    user_id TEXT,
    resource_id TEXT,
    status TEXT NOT NULL,
    supported_lane_id TEXT,
    historical_version_id TEXT,
    tax_year INTEGER,
    payload_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_orchestration_audit_events_event_id_not_blank CHECK (
        char_length(btrim(event_id)) > 0
    ),
    CONSTRAINT chk_orchestration_audit_events_event_type_not_blank CHECK (
        char_length(btrim(event_type)) > 0
    ),
    CONSTRAINT chk_orchestration_audit_events_status_not_blank CHECK (
        char_length(btrim(status)) > 0
    ),
    CONSTRAINT chk_orchestration_audit_events_tax_year_bounds CHECK (
        tax_year IS NULL OR tax_year BETWEEN 1900 AND 2100
    )
);

CREATE INDEX IF NOT EXISTS idx_orchestration_audit_events_correlation_time
    ON orchestration_audit_events (correlation_id, event_time, event_id);

CREATE INDEX IF NOT EXISTS idx_orchestration_audit_events_event_type_time
    ON orchestration_audit_events (event_type, event_time, event_id);

COMMIT;
