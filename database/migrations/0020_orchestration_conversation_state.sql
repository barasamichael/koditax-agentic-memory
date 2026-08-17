BEGIN;

CREATE TABLE IF NOT EXISTS orchestration_conversation_state_records (
    execution_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id TEXT,
    context_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_orchestration_conversation_state_execution_id_not_blank CHECK (
        char_length(btrim(execution_id)) > 0
    ),
    CONSTRAINT chk_orchestration_conversation_state_tenant_id_not_blank CHECK (
        char_length(btrim(tenant_id)) > 0
    ),
    CONSTRAINT chk_orchestration_conversation_state_conversation_id_not_blank CHECK (
        char_length(btrim(conversation_id)) > 0
    )
);

CREATE INDEX IF NOT EXISTS idx_orchestration_conversation_state_conversation_time
    ON orchestration_conversation_state_records (
        tenant_id,
        conversation_id,
        created_at DESC,
        execution_id DESC
    );

CREATE INDEX IF NOT EXISTS idx_orchestration_conversation_state_user_time
    ON orchestration_conversation_state_records (
        tenant_id,
        conversation_id,
        user_id,
        created_at DESC,
        execution_id DESC
    );

COMMIT;
