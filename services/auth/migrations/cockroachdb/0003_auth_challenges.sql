-- CockroachDB auth challenge and step-up persistence.

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
    CONSTRAINT uq_auth_otp_challenges_channel_idempotency UNIQUE (channel, idempotency_key),
    CONSTRAINT ck_auth_otp_challenges_channel CHECK (
        channel IN ('sms', 'email')
    ),
    CONSTRAINT ck_auth_otp_challenges_expires_after_issue CHECK (
        expires_at > issued_at
    ),
    CONSTRAINT ck_auth_otp_challenges_consumed_after_issue CHECK (
        consumed_at IS NULL OR consumed_at >= issued_at
    ),
    CONSTRAINT ck_auth_otp_challenges_cooldown_after_issue CHECK (
        cooldown_expires_at IS NULL OR cooldown_expires_at >= issued_at
    ),
    CONSTRAINT ck_auth_otp_challenges_failed_attempt_count_non_negative CHECK (
        failed_attempt_count >= 0
    ),
    CONSTRAINT ck_auth_otp_challenges_max_attempts_positive CHECK (
        max_attempts > 0
    )
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
    user_id UUID REFERENCES users (id) ON DELETE SET NULL,
    reset_code TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    failed_attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_auth_password_reset_challenges_idempotency_key UNIQUE (
        idempotency_key
    ),
    CONSTRAINT ck_auth_password_reset_channel CHECK (
        channel IN ('email', 'sms')
    ),
    CONSTRAINT ck_auth_password_reset_expires_after_issue CHECK (
        expires_at > issued_at
    ),
    CONSTRAINT ck_auth_password_reset_consumed_after_issue CHECK (
        consumed_at IS NULL OR consumed_at >= issued_at
    ),
    CONSTRAINT ck_auth_password_reset_failed_attempt_count_non_negative CHECK (
        failed_attempt_count >= 0
    ),
    CONSTRAINT ck_auth_password_reset_max_attempts_positive CHECK (
        max_attempts > 0
    )
);

CREATE INDEX IF NOT EXISTS idx_auth_password_reset_subject_issue
    ON auth_password_reset_challenges (channel, subject_normalized, issued_at);

CREATE TABLE IF NOT EXISTS auth_login_step_up_states (
    login_id_normalized TEXT NOT NULL,
    source_ip TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    challenge_id UUID NOT NULL REFERENCES auth_otp_challenges (challenge_id) ON DELETE RESTRICT,
    challenge_channel TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    challenge_expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_auth_login_step_up_states PRIMARY KEY (login_id_normalized, source_ip),
    CONSTRAINT uq_auth_login_step_up_states_challenge_id UNIQUE (challenge_id),
    CONSTRAINT ck_auth_login_step_up_states_channel_allowed CHECK (
        challenge_channel IN ('email', 'sms')
    ),
    CONSTRAINT ck_auth_login_step_up_states_expires_after_issue CHECK (
        challenge_expires_at > issued_at
    ),
    CONSTRAINT ck_auth_login_step_up_states_consumed_after_issue CHECK (
        consumed_at IS NULL OR consumed_at >= issued_at
    )
);

CREATE INDEX IF NOT EXISTS idx_auth_login_step_up_states_user_id
    ON auth_login_step_up_states (user_id, challenge_expires_at, updated_at);

CREATE INDEX IF NOT EXISTS idx_auth_login_step_up_states_expires_at
    ON auth_login_step_up_states (challenge_expires_at, updated_at);
