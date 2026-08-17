BEGIN;

ALTER TABLE audit_events
    ALTER COLUMN user_id DROP NOT NULL;

ALTER TABLE audit_events
    ALTER COLUMN role_at_time DROP NOT NULL;

CREATE OR REPLACE FUNCTION fn_audit_events_previous_hash_presence_check(
    _user_id UUID,
    _resource_type TEXT,
    _resource_id UUID,
    _previous_event_hash TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    has_prior_chain_event BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1
        FROM audit_events
        WHERE user_id IS NOT DISTINCT FROM _user_id
          AND resource_type = _resource_type
          AND resource_id IS NOT DISTINCT FROM _resource_id
    ) INTO has_prior_chain_event;

    IF has_prior_chain_event THEN
        RETURN _previous_event_hash IS NOT NULL;
    END IF;

    RETURN _previous_event_hash IS NULL;
END;
$$;

CREATE OR REPLACE FUNCTION fn_audit_events_canonical_payload(
    _user_id UUID,
    _resource_type TEXT,
    _resource_id UUID,
    _event_type TEXT,
    _event_timestamp TIMESTAMPTZ,
    _correlation_id TEXT
)
RETURNS TEXT
LANGUAGE sql
AS $$
    SELECT
        COALESCE(_user_id::TEXT, '')
        || '|'
        || _resource_type::TEXT
        || '|'
        || COALESCE(_resource_id::TEXT, '')
        || '|'
        || _event_type::TEXT
        || '|'
        || _event_timestamp::TEXT
        || '|'
        || _correlation_id::TEXT;
$$;

CREATE OR REPLACE FUNCTION fn_audit_events_enforce_hash_chain()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    canonical_payload TEXT;
    latest_event_hash TEXT;
    expected_event_hash TEXT;
BEGIN
    canonical_payload := fn_audit_events_canonical_payload(
        NEW.user_id,
        NEW.resource_type,
        NEW.resource_id,
        NEW.event_type,
        NEW.event_timestamp,
        NEW.correlation_id
    );

    SELECT event_hash INTO latest_event_hash
    FROM audit_events
    WHERE user_id IS NOT DISTINCT FROM NEW.user_id
      AND resource_type = NEW.resource_type
      AND resource_id IS NOT DISTINCT FROM NEW.resource_id
    ORDER BY event_timestamp DESC, created_at DESC, id DESC
    LIMIT 1;

    IF latest_event_hash IS NULL THEN
        IF NEW.previous_event_hash IS NOT NULL THEN
            RAISE EXCEPTION 'first audit event must have NULL previous_event_hash';
        END IF;
        expected_event_hash := encode(digest(canonical_payload, 'sha256'), 'hex');
    ELSE
        IF NEW.previous_event_hash IS NULL THEN
            RAISE EXCEPTION 'subsequent audit event requires previous_event_hash';
        END IF;
        IF NEW.previous_event_hash <> latest_event_hash THEN
            RAISE EXCEPTION 'previous_event_hash does not match latest chain hash';
        END IF;
        expected_event_hash := encode(
            digest(NEW.previous_event_hash || canonical_payload, 'sha256'),
            'hex'
        );
    END IF;

    IF NEW.event_hash IS NOT NULL AND NEW.event_hash <> expected_event_hash THEN
        RAISE EXCEPTION 'event_hash does not match deterministic hash chain';
    END IF;

    NEW.event_hash := expected_event_hash;
    RETURN NEW;
END;
$$;

COMMIT;
