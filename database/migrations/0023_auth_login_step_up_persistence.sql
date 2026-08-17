BEGIN;

CREATE TABLE IF NOT EXISTS auth_login_step_up_states (
    login_id_normalized TEXT NOT NULL,
    source_ip TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    challenge_id UUID NOT NULL UNIQUE REFERENCES auth_otp_challenges(challenge_id) ON DELETE RESTRICT,
    challenge_channel TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    challenge_expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_auth_login_step_up_states PRIMARY KEY (login_id_normalized, source_ip),
    CONSTRAINT ck_auth_login_step_up_states_channel_allowed
        CHECK (challenge_channel IN ('email', 'sms')),
    CONSTRAINT ck_auth_login_step_up_states_expires_after_issue
        CHECK (challenge_expires_at > issued_at),
    CONSTRAINT ck_auth_login_step_up_states_consumed_after_issue
        CHECK (consumed_at IS NULL OR consumed_at >= issued_at)
);

CREATE INDEX IF NOT EXISTS idx_auth_login_step_up_states_user_id
    ON auth_login_step_up_states (user_id, challenge_expires_at, updated_at);

CREATE INDEX IF NOT EXISTS idx_auth_login_step_up_states_expires_at
    ON auth_login_step_up_states (challenge_expires_at, updated_at);

COMMIT;
