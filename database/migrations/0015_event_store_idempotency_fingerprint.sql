BEGIN;

ALTER TABLE audit_events
    ADD COLUMN IF NOT EXISTS idempotency_payload_fingerprint TEXT;

CREATE INDEX IF NOT EXISTS idx_audit_events_idempotency_payload_fingerprint
    ON audit_events (idempotency_payload_fingerprint)
    WHERE idempotency_payload_fingerprint IS NOT NULL;

COMMIT;
