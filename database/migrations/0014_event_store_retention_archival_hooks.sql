BEGIN;

ALTER TABLE audit_events
    ADD COLUMN IF NOT EXISTS retention_policy_code TEXT NOT NULL DEFAULT 'event_store_default_retention';

ALTER TABLE audit_events
    ADD COLUMN IF NOT EXISTS retention_days INTEGER NOT NULL DEFAULT 3650;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_audit_events_retention_days_positive'
          AND conrelid = 'audit_events'::regclass
    ) THEN
        ALTER TABLE audit_events
            ADD CONSTRAINT chk_audit_events_retention_days_positive
            CHECK (retention_days > 0);
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS audit_event_archivals (
    event_id UUID PRIMARY KEY,
    archived_by_user_id UUID NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL,
    archival_reason_code TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_audit_event_archivals_event_id_audit_events
        FOREIGN KEY (event_id) REFERENCES audit_events (id) ON DELETE RESTRICT,
    CONSTRAINT fk_audit_event_archivals_archived_by_user_id_users
        FOREIGN KEY (archived_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_audit_event_archivals_archived_at
    ON audit_event_archivals (archived_at, event_id);

COMMIT;
