BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_audit_events_previous_event_hash_presence'
          AND conrelid = 'audit_events'::regclass
    ) THEN
        ALTER TABLE audit_events
            DROP CONSTRAINT chk_audit_events_previous_event_hash_presence;
    END IF;
END;
$$;

DROP FUNCTION IF EXISTS fn_audit_events_previous_hash_presence_check(UUID, TEXT, UUID, TEXT);

COMMIT;
