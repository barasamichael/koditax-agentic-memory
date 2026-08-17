BEGIN;

CREATE TABLE IF NOT EXISTS auth_idempotency_preclaims (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS auth_otp_challenges (
    challenge_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel TEXT NOT NULL,
    purpose TEXT NOT NULL,
    subject_normalized TEXT NOT NULL,
    otp_code TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    failed_attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    cooldown_seconds INTEGER NOT NULL,
    cooldown_expires_at TIMESTAMPTZ,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_auth_otp_challenges_channel CHECK (channel IN ('sms', 'email')),
    CONSTRAINT ck_auth_otp_challenges_expires_after_issue CHECK (expires_at > issued_at),
    CONSTRAINT ck_auth_otp_challenges_failed_attempt_count_non_negative CHECK (failed_attempt_count >= 0),
    CONSTRAINT ck_auth_otp_challenges_max_attempts_positive CHECK (max_attempts > 0),
    CONSTRAINT uq_auth_otp_challenges_channel_idempotency UNIQUE (channel, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_auth_otp_challenges_subject_issue
    ON auth_otp_challenges (channel, purpose, subject_normalized, issued_at);

CREATE INDEX IF NOT EXISTS idx_auth_otp_challenges_cooldown
    ON auth_otp_challenges (channel, purpose, subject_normalized, cooldown_expires_at);

CREATE TABLE IF NOT EXISTS auth_password_reset_challenges (
    challenge_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purpose TEXT NOT NULL,
    channel TEXT NOT NULL,
    subject_normalized TEXT NOT NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    reset_code TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    failed_attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_auth_password_reset_channel CHECK (channel IN ('email', 'sms')),
    CONSTRAINT ck_auth_password_reset_expires_after_issue CHECK (expires_at > issued_at),
    CONSTRAINT ck_auth_password_reset_failed_attempt_count_non_negative CHECK (failed_attempt_count >= 0),
    CONSTRAINT ck_auth_password_reset_max_attempts_positive CHECK (max_attempts > 0)
);

CREATE INDEX IF NOT EXISTS idx_auth_password_reset_subject_issue
    ON auth_password_reset_challenges (channel, subject_normalized, issued_at);

CREATE TABLE IF NOT EXISTS auth_phone_change_requests (
    request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    tenant_id TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    current_phone_number_normalized TEXT NOT NULL,
    new_phone_number_normalized TEXT NOT NULL,
    phone_change_state TEXT NOT NULL,
    step_up_challenge_id UUID NOT NULL REFERENCES auth_otp_challenges(challenge_id) ON DELETE RESTRICT,
    step_up_expires_at TIMESTAMPTZ NOT NULL,
    request_idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    confirmed_at TIMESTAMPTZ,
    confirm_idempotency_key TEXT UNIQUE,
    confirm_request_fingerprint TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_auth_phone_change_state CHECK (phone_change_state IN ('pending_confirmation', 'confirmed'))
);

CREATE INDEX IF NOT EXISTS idx_auth_phone_change_requests_user_requested
    ON auth_phone_change_requests (user_id, requested_at);

CREATE TABLE IF NOT EXISTS auth_phone_change_audit_events (
    audit_evidence_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    request_id UUID NOT NULL REFERENCES auth_phone_change_requests(request_id) ON DELETE CASCADE,
    phone_change_state TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT,
    trace_ref TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_phone_change_audit_user_time
    ON auth_phone_change_audit_events (user_id, occurred_at, created_at);

CREATE TABLE IF NOT EXISTS auth_account_deletion_requests (
    request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    tenant_id TEXT NOT NULL,
    request_reason TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    deletion_state TEXT NOT NULL,
    blocker_reasons JSONB NOT NULL DEFAULT '[]'::JSONB,
    request_idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    confirmed_at TIMESTAMPTZ,
    cooldown_expires_at TIMESTAMPTZ,
    executed_at TIMESTAMPTZ,
    execution_outcome TEXT,
    revoked_session_count INTEGER,
    confirm_idempotency_key TEXT UNIQUE,
    confirm_request_fingerprint TEXT,
    cancel_idempotency_key TEXT UNIQUE,
    cancel_request_fingerprint TEXT,
    execute_idempotency_key TEXT UNIQUE,
    execute_request_fingerprint TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_auth_account_deletion_state CHECK (deletion_state IN ('requested', 'blocked', 'confirmed', 'cancelled', 'executed')),
    CONSTRAINT ck_auth_account_deletion_execution_outcome CHECK (execution_outcome IS NULL OR execution_outcome IN ('tombstoned'))
);

CREATE INDEX IF NOT EXISTS idx_auth_account_deletion_requests_user_requested
    ON auth_account_deletion_requests (user_id, requested_at);

CREATE TABLE IF NOT EXISTS auth_account_deletion_audit_events (
    audit_evidence_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    request_id UUID NOT NULL REFERENCES auth_account_deletion_requests(request_id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    action_status TEXT NOT NULL,
    deletion_state TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    correlation_id TEXT,
    blocker_reasons JSONB NOT NULL DEFAULT '[]'::JSONB,
    reason_code TEXT,
    trace_ref TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_account_deletion_audit_user_time
    ON auth_account_deletion_audit_events (user_id, occurred_at, created_at);

CREATE TABLE IF NOT EXISTS auth_account_deletion_notifications (
    notification_id TEXT PRIMARY KEY,
    request_id UUID NOT NULL REFERENCES auth_account_deletion_requests(request_id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL,
    event_type TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
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
    actor_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
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
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    tenant_id TEXT NOT NULL,
    request_id UUID NOT NULL REFERENCES auth_account_deletion_requests(request_id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_account_deletion_reauth_user_request
    ON auth_account_deletion_reauth_proofs (user_id, request_id, expires_at);

CREATE TABLE IF NOT EXISTS auth_account_deletion_otp_proofs (
    otp_verification_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    tenant_id TEXT NOT NULL,
    request_id UUID NOT NULL REFERENCES auth_account_deletion_requests(request_id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_account_deletion_otp_user_request
    ON auth_account_deletion_otp_proofs (user_id, request_id, expires_at);

COMMIT;
