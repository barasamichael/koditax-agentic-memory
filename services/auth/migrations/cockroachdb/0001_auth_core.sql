-- CockroachDB auth core schema for Kodi Solutions AI Platform.

CREATE TABLE IF NOT EXISTS users (
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
    password_hash TEXT,
    password_history_hashes JSONB NOT NULL DEFAULT '[]'::JSONB,
    account_state TEXT NOT NULL DEFAULT 'pending_verification',
    verification_state TEXT NOT NULL DEFAULT 'pending_verification',
    verified_at TIMESTAMPTZ,
    credentials_invalidated_at TIMESTAMPTZ,
    deletion_lifecycle_state TEXT NOT NULL DEFAULT 'none',
    anonymized_at TIMESTAMPTZ,
    CONSTRAINT uq_users_phone_number_encrypted UNIQUE (phone_number_encrypted),
    CONSTRAINT uq_users_email_encrypted UNIQUE (email_encrypted),
    CONSTRAINT chk_users_exactly_one_role CHECK (
        role IN ('IndividualTaxpayer', 'TaxAgent', 'Accountant', 'Administrator')
    ),
    CONSTRAINT chk_users_account_state_allowed CHECK (
        account_state IN ('pending_verification', 'active', 'locked', 'disabled')
    ),
    CONSTRAINT chk_users_verification_state_allowed CHECK (
        verification_state IN ('pending_verification', 'verified')
    ),
    CONSTRAINT chk_users_deletion_lifecycle_state_allowed CHECK (
        deletion_lifecycle_state IN ('none', 'tombstoned')
    )
);

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS password_hash TEXT;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS password_history_hashes JSONB;

ALTER TABLE users
    ALTER COLUMN password_history_hashes SET DEFAULT '[]'::JSONB;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS account_state TEXT;

ALTER TABLE users
    ALTER COLUMN account_state SET DEFAULT 'pending_verification';

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS verification_state TEXT;

ALTER TABLE users
    ALTER COLUMN verification_state SET DEFAULT 'pending_verification';

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS credentials_invalidated_at TIMESTAMPTZ;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS deletion_lifecycle_state TEXT;

ALTER TABLE users
    ALTER COLUMN deletion_lifecycle_state SET DEFAULT 'none';

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS anonymized_at TIMESTAMPTZ;

UPDATE users
SET password_history_hashes = COALESCE(password_history_hashes, '[]'::JSONB),
    account_state = COALESCE(account_state, 'pending_verification'),
    verification_state = COALESCE(verification_state, 'pending_verification'),
    deletion_lifecycle_state = COALESCE(deletion_lifecycle_state, 'none');

ALTER TABLE users
    ALTER COLUMN password_history_hashes SET NOT NULL;

ALTER TABLE users
    ALTER COLUMN account_state SET NOT NULL;

ALTER TABLE users
    ALTER COLUMN verification_state SET NOT NULL;

ALTER TABLE users
    ALTER COLUMN deletion_lifecycle_state SET NOT NULL;

ALTER TABLE users
    ADD CONSTRAINT IF NOT EXISTS uq_users_phone_number_encrypted UNIQUE (phone_number_encrypted);

ALTER TABLE users
    ADD CONSTRAINT IF NOT EXISTS uq_users_email_encrypted UNIQUE (email_encrypted);

ALTER TABLE users
    ADD CONSTRAINT IF NOT EXISTS chk_users_exactly_one_role CHECK (
        role IN ('IndividualTaxpayer', 'TaxAgent', 'Accountant', 'Administrator')
    );

ALTER TABLE users
    ADD CONSTRAINT IF NOT EXISTS chk_users_account_state_allowed CHECK (
        account_state IN ('pending_verification', 'active', 'locked', 'disabled')
    );

ALTER TABLE users
    ADD CONSTRAINT IF NOT EXISTS chk_users_verification_state_allowed CHECK (
        verification_state IN ('pending_verification', 'verified')
    );

ALTER TABLE users
    ADD CONSTRAINT IF NOT EXISTS chk_users_deletion_lifecycle_state_allowed CHECK (
        deletion_lifecycle_state IN ('none', 'tombstoned')
    );

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    inactivity_expires_at TIMESTAMPTZ NOT NULL,
    last_activity_at TIMESTAMPTZ NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default_tenant',
    role TEXT NOT NULL DEFAULT 'IndividualTaxpayer',
    device_fingerprint_hash TEXT,
    is_invalidated BOOLEAN NOT NULL DEFAULT FALSE,
    invalidated_at TIMESTAMPTZ,
    invalidated_reason TEXT,
    access_token_hash TEXT,
    refresh_token_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_sessions_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT chk_sessions_expires_after_issue CHECK (expires_at > issued_at),
    CONSTRAINT chk_sessions_inactivity_expires_after_issue CHECK (
        inactivity_expires_at > issued_at
    ),
    CONSTRAINT chk_sessions_last_activity_after_issue CHECK (
        last_activity_at >= issued_at
    ),
    CONSTRAINT chk_sessions_invalidated_at_consistency CHECK (
        (
            is_invalidated = FALSE
            AND invalidated_at IS NULL
            AND invalidated_reason IS NULL
        )
        OR (
            is_invalidated = TRUE
            AND invalidated_at IS NOT NULL
            AND invalidated_reason IN (
                'session_concurrency_limit_enforced',
                'session_revoked'
            )
        )
    )
);

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS tenant_id TEXT;

ALTER TABLE sessions
    ALTER COLUMN tenant_id SET DEFAULT 'default_tenant';

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS role TEXT;

ALTER TABLE sessions
    ALTER COLUMN role SET DEFAULT 'IndividualTaxpayer';

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS inactivity_expires_at TIMESTAMPTZ;

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ;

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS invalidated_reason TEXT;

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS access_token_hash TEXT;

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS refresh_token_hash TEXT;

UPDATE sessions
SET inactivity_expires_at = COALESCE(inactivity_expires_at, expires_at),
    last_activity_at = COALESCE(last_activity_at, issued_at),
    tenant_id = COALESCE(tenant_id, 'default_tenant'),
    role = COALESCE(role, 'IndividualTaxpayer')
WHERE inactivity_expires_at IS NULL
   OR last_activity_at IS NULL
   OR tenant_id IS NULL
   OR role IS NULL;

ALTER TABLE sessions
    ALTER COLUMN tenant_id SET NOT NULL;

ALTER TABLE sessions
    ALTER COLUMN role SET NOT NULL;

ALTER TABLE sessions
    ALTER COLUMN inactivity_expires_at SET NOT NULL;

ALTER TABLE sessions
    ALTER COLUMN last_activity_at SET NOT NULL;

ALTER TABLE sessions
    ADD CONSTRAINT IF NOT EXISTS uq_sessions_idempotency_key UNIQUE (idempotency_key);

ALTER TABLE sessions
    ADD CONSTRAINT IF NOT EXISTS chk_sessions_expires_after_issue CHECK (
        expires_at > issued_at
    );

ALTER TABLE sessions
    ADD CONSTRAINT IF NOT EXISTS chk_sessions_inactivity_expires_after_issue CHECK (
        inactivity_expires_at > issued_at
    );

ALTER TABLE sessions
    ADD CONSTRAINT IF NOT EXISTS chk_sessions_last_activity_after_issue CHECK (
        last_activity_at >= issued_at
    );

ALTER TABLE sessions
    ADD CONSTRAINT IF NOT EXISTS chk_sessions_invalidated_at_consistency CHECK (
        (
            is_invalidated = FALSE
            AND invalidated_at IS NULL
            AND invalidated_reason IS NULL
        )
        OR (
            is_invalidated = TRUE
            AND invalidated_at IS NOT NULL
            AND invalidated_reason IN (
                'session_concurrency_limit_enforced',
                'session_revoked'
            )
        )
    );

ALTER TABLE sessions
    ADD CONSTRAINT IF NOT EXISTS chk_sessions_role_allowed CHECK (
        role IN ('IndividualTaxpayer', 'TaxAgent', 'Accountant', 'Administrator')
    );

CREATE INDEX IF NOT EXISTS idx_sessions_user_issued_at
    ON sessions (user_id, issued_at, id);

CREATE INDEX IF NOT EXISTS idx_sessions_access_token_hash
    ON sessions (access_token_hash)
    WHERE access_token_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sessions_refresh_token_hash
    ON sessions (refresh_token_hash)
    WHERE refresh_token_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS delegations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_user_id UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    delegate_user_id UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_delegations_distinct_users CHECK (
        principal_user_id <> delegate_user_id
    ),
    CONSTRAINT chk_delegations_revoked_after_granted CHECK (
        revoked_at IS NULL OR revoked_at >= granted_at
    ),
    CONSTRAINT chk_delegations_active_revocation_consistency CHECK (
        (is_active = TRUE AND revoked_at IS NULL)
        OR (is_active = FALSE AND revoked_at IS NOT NULL)
    )
);

ALTER TABLE delegations
    ADD CONSTRAINT IF NOT EXISTS chk_delegations_distinct_users CHECK (
        principal_user_id <> delegate_user_id
    );

ALTER TABLE delegations
    ADD CONSTRAINT IF NOT EXISTS chk_delegations_revoked_after_granted CHECK (
        revoked_at IS NULL OR revoked_at >= granted_at
    );

ALTER TABLE delegations
    ADD CONSTRAINT IF NOT EXISTS chk_delegations_active_revocation_consistency CHECK (
        (is_active = TRUE AND revoked_at IS NULL)
        OR (is_active = FALSE AND revoked_at IS NOT NULL)
    );

CREATE UNIQUE INDEX IF NOT EXISTS uq_delegations_active_pair
    ON delegations (principal_user_id, delegate_user_id)
    WHERE is_active;

