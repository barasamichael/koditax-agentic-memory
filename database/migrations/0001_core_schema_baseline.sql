-- Kodi core schema baseline migration.
-- Decision: PostgreSQL 15+ is the target engine (uses pgcrypto for UUID generation).
-- Decision: users.role is a single constrained column to enforce exactly one role per user.
-- Decision: document state transitions are DB-enforced through trigger logic.
-- Decision: idempotency_key is UNIQUE on sessions, computations, and submissions.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_number_encrypted TEXT NOT NULL,
    email_encrypted TEXT NOT NULL,
    kra_pin_encrypted TEXT,
    role TEXT NOT NULL,
    subscription_tier TEXT NOT NULL DEFAULT 'standard',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_locked BOOLEAN NOT NULL DEFAULT FALSE,
    lock_expires_at TIMESTAMPTZ,
    CONSTRAINT uq_users_phone_number_encrypted UNIQUE (phone_number_encrypted),
    CONSTRAINT uq_users_email_encrypted UNIQUE (email_encrypted),
    CONSTRAINT chk_users_exactly_one_role CHECK (
        role IN ('IndividualTaxpayer', 'TaxAgent', 'Accountant', 'Administrator')
    )
);

CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    idempotency_key TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    device_fingerprint_hash TEXT,
    is_invalidated BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_sessions_user_id_users
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_sessions_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT chk_sessions_expires_after_issue CHECK (expires_at > issued_at)
);

CREATE TABLE delegations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_user_id UUID NOT NULL,
    delegate_user_id UUID NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_delegations_principal_user_id_users
        FOREIGN KEY (principal_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_delegations_delegate_user_id_users
        FOREIGN KEY (delegate_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT chk_delegations_distinct_users CHECK (principal_user_id <> delegate_user_id),
    CONSTRAINT chk_delegations_revoked_after_granted CHECK (
        revoked_at IS NULL OR revoked_at >= granted_at
    )
);

CREATE UNIQUE INDEX uq_delegations_active_pair
    ON delegations (principal_user_id, delegate_user_id)
    WHERE is_active;

CREATE TABLE computations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    session_id UUID,
    tax_type TEXT NOT NULL,
    regime_type TEXT NOT NULL,
    regime_identifier TEXT,
    tax_year INTEGER NOT NULL,
    rule_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retention_expires_at TIMESTAMPTZ NOT NULL,
    compliance_lock_until TIMESTAMPTZ NOT NULL,
    CONSTRAINT fk_computations_user_id_users
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_computations_session_id_sessions
        FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_computations_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT chk_computations_tax_year_bounds CHECK (tax_year BETWEEN 2000 AND 2100),
    CONSTRAINT chk_computations_regime_type CHECK (
        regime_type IN ('income_tax', 'health_tax', 'vat', 'other')
    ),
    CONSTRAINT chk_computations_health_tax_regime_identifier CHECK (
        regime_type <> 'health_tax' OR regime_identifier IS NOT NULL
    )
);

CREATE TABLE computation_results (
    computation_id UUID PRIMARY KEY,
    result_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_computation_results_computation_id_computations
        FOREIGN KEY (computation_id) REFERENCES computations (id) ON DELETE RESTRICT
);

CREATE TABLE validations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    computation_id UUID NOT NULL,
    validation_context TEXT NOT NULL,
    findings JSONB NOT NULL,
    validated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_validations_computation_id_computations
        FOREIGN KEY (computation_id) REFERENCES computations (id) ON DELETE RESTRICT
);

CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    computation_id UUID,
    storage_key TEXT NOT NULL,
    state TEXT NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    purge_eligible_at TIMESTAMPTZ,
    purged_at TIMESTAMPTZ,
    compliance_lock_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_documents_user_id_users
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_documents_computation_id_computations
        FOREIGN KEY (computation_id) REFERENCES computations (id) ON DELETE RESTRICT,
    CONSTRAINT chk_documents_state_allowed CHECK (
        state IN ('uploaded', 'processing', 'validated', 'eligible_for_purge', 'purged')
    ),
    CONSTRAINT chk_documents_purged_at_requires_purged_state CHECK (
        purged_at IS NULL OR state = 'purged'
    ),
    CONSTRAINT chk_documents_purged_state_requires_purged_at CHECK (
        state <> 'purged' OR purged_at IS NOT NULL
    )
);

CREATE TABLE document_extractions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL,
    extracted_fields JSONB NOT NULL,
    confidence_scores JSONB NOT NULL,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_document_extractions_document_id_documents
        FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_extractions_verified_at_consistency CHECK (
        (verified = FALSE AND verified_at IS NULL) OR
        (verified = TRUE AND verified_at IS NOT NULL)
    )
);

CREATE TABLE forms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    computation_id UUID NOT NULL,
    document_id UUID,
    form_type TEXT NOT NULL,
    form_version TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retention_expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_forms_user_id_users
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_forms_computation_id_computations
        FOREIGN KEY (computation_id) REFERENCES computations (id) ON DELETE RESTRICT,
    CONSTRAINT fk_forms_document_id_documents
        FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE RESTRICT
);

CREATE TABLE reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    computation_id UUID NOT NULL,
    form_id UUID,
    report_type TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    download_expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_reports_user_id_users
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_reports_computation_id_computations
        FOREIGN KEY (computation_id) REFERENCES computations (id) ON DELETE RESTRICT,
    CONSTRAINT fk_reports_form_id_forms
        FOREIGN KEY (form_id) REFERENCES forms (id) ON DELETE RESTRICT
);

CREATE TABLE audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    role_at_time TEXT NOT NULL,
    event_type TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id UUID,
    correlation_id TEXT NOT NULL,
    request_id TEXT,
    idempotency_key TEXT,
    details JSONB NOT NULL DEFAULT '{}'::JSONB,
    previous_event_hash TEXT,
    event_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retention_expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT fk_audit_events_user_id_users
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE TABLE submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    computation_id UUID NOT NULL,
    form_id UUID,
    report_id UUID,
    confirmation_event_id UUID NOT NULL,
    idempotency_key TEXT NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_submissions_user_id_users
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_submissions_computation_id_computations
        FOREIGN KEY (computation_id) REFERENCES computations (id) ON DELETE RESTRICT,
    CONSTRAINT fk_submissions_form_id_forms
        FOREIGN KEY (form_id) REFERENCES forms (id) ON DELETE RESTRICT,
    CONSTRAINT fk_submissions_report_id_reports
        FOREIGN KEY (report_id) REFERENCES reports (id) ON DELETE RESTRICT,
    CONSTRAINT fk_submissions_confirmation_event_id_audit_events
        FOREIGN KEY (confirmation_event_id) REFERENCES audit_events (id) ON DELETE RESTRICT,
    CONSTRAINT uq_submissions_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT chk_submissions_status CHECK (
        status IN ('pending', 'submitted', 'failed', 'confirmed')
    )
);

CREATE INDEX idx_sessions_user_id ON sessions (user_id);
CREATE INDEX idx_delegations_principal_user_id ON delegations (principal_user_id);
CREATE INDEX idx_delegations_delegate_user_id ON delegations (delegate_user_id);
CREATE INDEX idx_computations_user_id ON computations (user_id);
CREATE INDEX idx_computations_session_id ON computations (session_id);
CREATE INDEX idx_computations_tax_year ON computations (tax_year);
CREATE INDEX idx_validations_computation_id ON validations (computation_id);
CREATE INDEX idx_documents_user_id ON documents (user_id);
CREATE INDEX idx_documents_computation_id ON documents (computation_id);
CREATE INDEX idx_documents_user_id_state ON documents (user_id, state);
CREATE INDEX idx_document_extractions_document_id ON document_extractions (document_id);
CREATE INDEX idx_forms_user_id ON forms (user_id);
CREATE INDEX idx_forms_computation_id ON forms (computation_id);
CREATE INDEX idx_reports_user_id ON reports (user_id);
CREATE INDEX idx_reports_computation_id ON reports (computation_id);
CREATE INDEX idx_submissions_user_id ON submissions (user_id);
CREATE INDEX idx_submissions_computation_id ON submissions (computation_id);
CREATE INDEX idx_submissions_confirmation_event_id ON submissions (confirmation_event_id);
CREATE INDEX idx_audit_events_user_id_created_at ON audit_events (user_id, created_at);
CREATE INDEX idx_audit_events_correlation_id ON audit_events (correlation_id);
CREATE INDEX idx_audit_events_idempotency_key ON audit_events (idempotency_key);

CREATE OR REPLACE FUNCTION fn_audit_events_prevent_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$;

CREATE TRIGGER trg_audit_events_prevent_update
    BEFORE UPDATE ON audit_events
    FOR EACH ROW
    EXECUTE FUNCTION fn_audit_events_prevent_change();

CREATE TRIGGER trg_audit_events_prevent_delete
    BEFORE DELETE ON audit_events
    FOR EACH ROW
    EXECUTE FUNCTION fn_audit_events_prevent_change();

CREATE OR REPLACE FUNCTION fn_documents_enforce_state_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.state IS DISTINCT FROM OLD.state THEN
        IF NOT (
            (OLD.state = 'uploaded' AND NEW.state IN ('processing', 'eligible_for_purge')) OR
            (OLD.state = 'processing' AND NEW.state IN ('validated', 'eligible_for_purge')) OR
            (OLD.state = 'validated' AND NEW.state = 'eligible_for_purge') OR
            (OLD.state = 'eligible_for_purge' AND NEW.state = 'purged') OR
            (OLD.state = NEW.state)
        ) THEN
            RAISE EXCEPTION 'Invalid document state transition from % to %', OLD.state, NEW.state;
        END IF;
    END IF;

    IF NEW.purged_at IS NOT NULL THEN
        IF NEW.state <> 'purged' THEN
            RAISE EXCEPTION 'Cannot set purged_at unless document state is purged';
        END IF;
        IF OLD.state NOT IN ('eligible_for_purge', 'purged') THEN
            RAISE EXCEPTION 'Cannot purge document before eligible_for_purge state';
        END IF;
        IF NEW.purge_eligible_at IS NULL THEN
            RAISE EXCEPTION 'Cannot purge document without purge_eligible_at';
        END IF;
        IF NEW.purged_at < NEW.purge_eligible_at THEN
            RAISE EXCEPTION 'Cannot purge document before purge_eligible_at';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_documents_enforce_state_transition
    BEFORE UPDATE OF state, purged_at, purge_eligible_at ON documents
    FOR EACH ROW
    EXECUTE FUNCTION fn_documents_enforce_state_transition();

CREATE OR REPLACE FUNCTION fn_documents_prevent_delete_before_eligibility()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.state <> 'eligible_for_purge' THEN
        RAISE EXCEPTION 'Cannot hard-delete document unless in eligible_for_purge state';
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER trg_documents_prevent_delete_before_eligibility
    BEFORE DELETE ON documents
    FOR EACH ROW
    EXECUTE FUNCTION fn_documents_prevent_delete_before_eligibility();

COMMIT;
