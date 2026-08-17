-- CockroachDB auth phone-change and account-deletion lifecycle persistence.

CREATE TABLE IF NOT EXISTS auth_phone_change_requests (
    request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    tenant_id TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    current_phone_number_normalized TEXT NOT NULL,
    new_phone_number_normalized TEXT NOT NULL,
    phone_change_state TEXT NOT NULL,
    step_up_challenge_id UUID NOT NULL REFERENCES auth_otp_challenges (challenge_id) ON DELETE RESTRICT,
    step_up_expires_at TIMESTAMPTZ NOT NULL,
    request_idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    confirmed_at TIMESTAMPTZ,
    confirm_idempotency_key TEXT,
    confirm_request_fingerprint TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_auth_phone_change_requests_request_idempotency_key UNIQUE (
        request_idempotency_key
    ),
    CONSTRAINT uq_auth_phone_change_requests_confirm_idempotency_key UNIQUE (
        confirm_idempotency_key
    ),
    CONSTRAINT ck_auth_phone_change_state CHECK (
        phone_change_state IN ('pending_confirmation', 'superseded', 'confirmed')
    )
);

CREATE INDEX IF NOT EXISTS idx_auth_phone_change_requests_user_requested
    ON auth_phone_change_requests (user_id, requested_at, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_phone_change_requests_pending_user
    ON auth_phone_change_requests (user_id)
    WHERE phone_change_state = 'pending_confirmation';

CREATE TABLE IF NOT EXISTS auth_phone_change_audit_events (
    audit_evidence_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    request_id UUID NOT NULL REFERENCES auth_phone_change_requests (request_id) ON DELETE CASCADE,
    phone_change_state TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT,
    trace_ref TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_phone_change_audit_user_time
    ON auth_phone_change_audit_events (user_id, occurred_at, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_phone_change_audit_events_event_id
    ON auth_phone_change_audit_events (event_id);

CREATE TABLE IF NOT EXISTS auth_account_deletion_requests (
    request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    tenant_id TEXT NOT NULL,
    request_reason TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    deletion_state TEXT NOT NULL,
    blocker_reasons JSONB NOT NULL DEFAULT '[]'::JSONB,
    request_idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    confirmed_at TIMESTAMPTZ,
    cooldown_expires_at TIMESTAMPTZ,
    executed_at TIMESTAMPTZ,
    execution_outcome TEXT,
    revoked_session_count INTEGER,
    confirm_idempotency_key TEXT,
    confirm_request_fingerprint TEXT,
    cancel_idempotency_key TEXT,
    cancel_request_fingerprint TEXT,
    execute_idempotency_key TEXT,
    execute_request_fingerprint TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_auth_account_deletion_requests_request_idempotency_key UNIQUE (
        request_idempotency_key
    ),
    CONSTRAINT uq_auth_account_deletion_requests_confirm_idempotency_key UNIQUE (
        confirm_idempotency_key
    ),
    CONSTRAINT uq_auth_account_deletion_requests_cancel_idempotency_key UNIQUE (
        cancel_idempotency_key
    ),
    CONSTRAINT uq_auth_account_deletion_requests_execute_idempotency_key UNIQUE (
        execute_idempotency_key
    ),
    CONSTRAINT ck_auth_account_deletion_state CHECK (
        deletion_state IN ('requested', 'blocked', 'confirmed', 'cancelled', 'executed')
    ),
    CONSTRAINT ck_auth_account_deletion_execution_outcome CHECK (
        execution_outcome IS NULL OR execution_outcome IN ('tombstoned')
    ),
    CONSTRAINT ck_auth_account_deletion_revoked_session_count_non_negative CHECK (
        revoked_session_count IS NULL OR revoked_session_count >= 0
    )
);

CREATE INDEX IF NOT EXISTS idx_auth_account_deletion_requests_user_requested
    ON auth_account_deletion_requests (user_id, requested_at, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_account_deletion_requests_active_user
    ON auth_account_deletion_requests (user_id)
    WHERE deletion_state IN ('requested', 'blocked', 'confirmed');

CREATE TABLE IF NOT EXISTS auth_account_deletion_audit_events (
    audit_evidence_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    request_id UUID NOT NULL REFERENCES auth_account_deletion_requests (request_id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    action_status TEXT NOT NULL,
    deletion_state TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT,
    blocker_reasons JSONB NOT NULL DEFAULT '[]'::JSONB,
    reason_code TEXT,
    trace_ref TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_account_deletion_audit_user_time
    ON auth_account_deletion_audit_events (user_id, occurred_at, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_account_deletion_audit_events_event_id
    ON auth_account_deletion_audit_events (event_id);

CREATE TABLE IF NOT EXISTS auth_account_deletion_notifications (
    notification_id TEXT PRIMARY KEY,
    request_id UUID NOT NULL REFERENCES auth_account_deletion_requests (request_id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL,
    event_type TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    deletion_state TEXT NOT NULL,
    correlation_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_account_deletion_notifications_user_time
    ON auth_account_deletion_notifications (user_id, attempted_at, created_at);

CREATE TABLE IF NOT EXISTS auth_account_deletion_incidents (
    audit_reference_id TEXT PRIMARY KEY,
    incident_code TEXT NOT NULL,
    message TEXT NOT NULL,
    reason TEXT NOT NULL,
    request_id UUID NOT NULL,
    actor_user_id UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    tenant_id TEXT NOT NULL,
    account_deletion_state TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_account_deletion_incidents_user_time
    ON auth_account_deletion_incidents (actor_user_id, occurred_at, created_at);

CREATE TABLE IF NOT EXISTS auth_account_deletion_reauth_proofs (
    proof_id TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    tenant_id TEXT NOT NULL,
    request_id UUID NOT NULL REFERENCES auth_account_deletion_requests (request_id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_account_deletion_reauth_user_request
    ON auth_account_deletion_reauth_proofs (user_id, request_id, expires_at);

CREATE TABLE IF NOT EXISTS auth_account_deletion_otp_proofs (
    otp_verification_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    tenant_id TEXT NOT NULL,
    request_id UUID NOT NULL REFERENCES auth_account_deletion_requests (request_id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_account_deletion_otp_user_request
    ON auth_account_deletion_otp_proofs (user_id, request_id, expires_at);
