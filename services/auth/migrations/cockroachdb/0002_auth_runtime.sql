-- CockroachDB auth runtime schema for sessions and login lockouts.

CREATE TABLE IF NOT EXISTS auth_session_refresh_tokens (
    refresh_token_hash TEXT PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    issued_at TIMESTAMPTZ NOT NULL,
    is_consumed BOOLEAN NOT NULL DEFAULT FALSE,
    consumed_at TIMESTAMPTZ,
    CONSTRAINT chk_auth_session_refresh_tokens_consumed_consistency CHECK (
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
    CONSTRAINT chk_auth_login_lockouts_failed_attempt_count_non_negative CHECK (
        failed_attempt_count >= 0
    )
);

CREATE INDEX IF NOT EXISTS idx_auth_login_lockouts_expires_at
    ON auth_login_lockouts (lockout_expires_at);

