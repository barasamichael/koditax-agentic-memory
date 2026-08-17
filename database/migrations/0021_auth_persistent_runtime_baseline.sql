BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS password_hash TEXT,
    ADD COLUMN IF NOT EXISTS password_history_hashes JSONB NOT NULL DEFAULT '[]'::JSONB,
    ADD COLUMN IF NOT EXISTS account_state TEXT NOT NULL DEFAULT 'pending_verification',
    ADD COLUMN IF NOT EXISTS verification_state TEXT NOT NULL DEFAULT 'pending_verification',
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS credentials_invalidated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deletion_lifecycle_state TEXT NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS anonymized_at TIMESTAMPTZ;

ALTER TABLE users
    DROP CONSTRAINT IF EXISTS chk_users_account_state_allowed;

ALTER TABLE users
    ADD CONSTRAINT chk_users_account_state_allowed
    CHECK (account_state IN ('pending_verification', 'active', 'locked', 'disabled'));

ALTER TABLE users
    DROP CONSTRAINT IF EXISTS chk_users_verification_state_allowed;

ALTER TABLE users
    ADD CONSTRAINT chk_users_verification_state_allowed
    CHECK (verification_state IN ('pending_verification', 'verified'));

ALTER TABLE users
    DROP CONSTRAINT IF EXISTS chk_users_deletion_lifecycle_state_allowed;

ALTER TABLE users
    ADD CONSTRAINT chk_users_deletion_lifecycle_state_allowed
    CHECK (deletion_lifecycle_state IN ('none', 'tombstoned'));

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default_tenant',
    ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'IndividualTaxpayer',
    ADD COLUMN IF NOT EXISTS inactivity_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS invalidated_reason TEXT,
    ADD COLUMN IF NOT EXISTS access_token_hash TEXT,
    ADD COLUMN IF NOT EXISTS refresh_token_hash TEXT;

UPDATE sessions
SET inactivity_expires_at = COALESCE(inactivity_expires_at, expires_at),
    last_activity_at = COALESCE(last_activity_at, issued_at)
WHERE inactivity_expires_at IS NULL
   OR last_activity_at IS NULL;

ALTER TABLE sessions
    ALTER COLUMN inactivity_expires_at SET NOT NULL;

ALTER TABLE sessions
    ALTER COLUMN last_activity_at SET NOT NULL;

ALTER TABLE sessions
    DROP CONSTRAINT IF EXISTS chk_sessions_invalidated_reason_allowed;

ALTER TABLE sessions
    ADD CONSTRAINT chk_sessions_invalidated_reason_allowed
    CHECK (
        invalidated_reason IS NULL
        OR invalidated_reason IN ('session_concurrency_limit_enforced', 'session_revoked')
    );

CREATE TABLE IF NOT EXISTS auth_session_refresh_tokens (
    refresh_token_hash TEXT PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    issued_at TIMESTAMPTZ NOT NULL,
    is_consumed BOOLEAN NOT NULL DEFAULT FALSE,
    consumed_at TIMESTAMPTZ,
    CONSTRAINT chk_auth_session_refresh_tokens_consumed_consistency
        CHECK (
            (is_consumed = FALSE AND consumed_at IS NULL)
            OR (is_consumed = TRUE AND consumed_at IS NOT NULL)
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_session_refresh_tokens_active_session
    ON auth_session_refresh_tokens (session_id)
    WHERE is_consumed = FALSE;

CREATE INDEX IF NOT EXISTS idx_auth_session_refresh_tokens_session_id
    ON auth_session_refresh_tokens (session_id);

CREATE TABLE IF NOT EXISTS auth_login_lockouts (
    login_id_normalized TEXT NOT NULL,
    source_ip TEXT NOT NULL,
    failed_attempt_count INTEGER NOT NULL DEFAULT 0,
    last_failed_attempt_at TIMESTAMPTZ,
    lockout_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_auth_login_lockouts PRIMARY KEY (login_id_normalized, source_ip),
    CONSTRAINT chk_auth_login_lockouts_failed_attempt_count_non_negative
        CHECK (failed_attempt_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_auth_login_lockouts_expires_at
    ON auth_login_lockouts (lockout_expires_at);

COMMIT;
