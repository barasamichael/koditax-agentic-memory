BEGIN;

ALTER TABLE audit_events
    ADD COLUMN event_timestamp TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE users
    ADD CONSTRAINT chk_users_created_at_not_future
        CHECK (created_at <= now());

ALTER TABLE sessions
    ADD CONSTRAINT chk_sessions_created_at_not_future
        CHECK (created_at <= now());

ALTER TABLE delegations
    ADD CONSTRAINT chk_delegations_created_at_not_future
        CHECK (created_at <= now());

ALTER TABLE computations
    ADD CONSTRAINT chk_computations_created_at_not_future
        CHECK (created_at <= now());

ALTER TABLE computation_results
    ADD CONSTRAINT chk_computation_results_created_at_not_future
        CHECK (created_at <= now());

ALTER TABLE validations
    ADD CONSTRAINT chk_validations_created_at_not_future
        CHECK (created_at <= now());

ALTER TABLE documents
    ADD CONSTRAINT chk_documents_created_at_not_future
        CHECK (created_at <= now());

ALTER TABLE document_extractions
    ADD CONSTRAINT chk_document_extractions_created_at_not_future
        CHECK (created_at <= now());

ALTER TABLE forms
    ADD CONSTRAINT chk_forms_created_at_not_future
        CHECK (created_at <= now());

ALTER TABLE reports
    ADD CONSTRAINT chk_reports_created_at_not_future
        CHECK (created_at <= now());

ALTER TABLE submissions
    ADD CONSTRAINT chk_submissions_created_at_not_future
        CHECK (created_at <= now());

ALTER TABLE audit_events
    ADD CONSTRAINT chk_audit_events_created_at_not_future
        CHECK (created_at <= now());

ALTER TABLE audit_events
    ADD CONSTRAINT chk_audit_events_event_timestamp_not_future
        CHECK (event_timestamp <= now());

CREATE OR REPLACE FUNCTION fn_audit_events_enforce_temporal_monotonicity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    max_prior_event_timestamp TIMESTAMPTZ;
BEGIN
    IF NEW.resource_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT MAX(event_timestamp) INTO max_prior_event_timestamp
    FROM audit_events
    WHERE user_id IS NOT DISTINCT FROM NEW.user_id
      AND resource_type = NEW.resource_type
      AND resource_id = NEW.resource_id;

    IF max_prior_event_timestamp IS NOT NULL
       AND NEW.event_timestamp < max_prior_event_timestamp THEN
        RAISE EXCEPTION 'audit event timestamp regression for aggregate';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_events_enforce_temporal_monotonicity ON audit_events;
CREATE TRIGGER trg_audit_events_enforce_temporal_monotonicity
    BEFORE INSERT ON audit_events
    FOR EACH ROW
    EXECUTE FUNCTION fn_audit_events_enforce_temporal_monotonicity();

CREATE OR REPLACE FUNCTION fn_documents_enforce_state_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.state = 'eligible_for_purge' THEN
        IF NEW.purge_eligible_at IS NULL THEN
            RAISE EXCEPTION 'eligible_for_purge requires purge_eligible_at';
        END IF;
        IF NEW.purge_eligible_at > now() THEN
            RAISE EXCEPTION 'eligible_for_purge requires past-or-present purge_eligible_at';
        END IF;
    END IF;

    IF NEW.purge_eligible_at IS NOT NULL AND NEW.purge_eligible_at < NEW.uploaded_at THEN
        RAISE EXCEPTION 'purge_eligible_at cannot be earlier than uploaded_at';
    END IF;

    IF TG_OP = 'UPDATE' AND NEW.state IS DISTINCT FROM OLD.state THEN
        IF NOT (
            (OLD.state = 'uploaded' AND NEW.state IN ('processing', 'eligible_for_purge')) OR
            (OLD.state = 'processing' AND NEW.state IN ('validated', 'eligible_for_purge')) OR
            (OLD.state = 'validated' AND NEW.state = 'eligible_for_purge') OR
            (OLD.state = 'eligible_for_purge' AND NEW.state = 'purged') OR
            (OLD.state = NEW.state)
        ) THEN
            RAISE EXCEPTION 'Invalid document state transition from % to %', OLD.state, NEW.state;
        END IF;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF NEW.purge_eligible_at IS NOT NULL
           AND OLD.purge_eligible_at IS NOT NULL
           AND NEW.purge_eligible_at < OLD.purge_eligible_at THEN
            RAISE EXCEPTION 'document purge_eligible_at cannot regress';
        END IF;
        IF NEW.purged_at IS NOT NULL
           AND OLD.purged_at IS NOT NULL
           AND NEW.purged_at < OLD.purged_at THEN
            RAISE EXCEPTION 'document purged_at cannot regress';
        END IF;
    END IF;

    IF NEW.purged_at IS NOT NULL THEN
        IF NEW.state <> 'purged' THEN
            RAISE EXCEPTION 'Cannot set purged_at unless document state is purged';
        END IF;
        IF NEW.purge_eligible_at IS NULL THEN
            RAISE EXCEPTION 'Cannot purge document without purge_eligible_at';
        END IF;
        IF TG_OP = 'UPDATE' AND OLD.state NOT IN ('eligible_for_purge', 'purged') THEN
            RAISE EXCEPTION 'Cannot purge document before eligible_for_purge state';
        END IF;
        IF NEW.purged_at < NEW.purge_eligible_at THEN
            RAISE EXCEPTION 'Cannot purge document before purge_eligible_at';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

COMMIT;
