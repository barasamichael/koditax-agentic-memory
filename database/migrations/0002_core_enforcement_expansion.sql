BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_events_idempotency_key_not_null
    ON audit_events (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE OR REPLACE FUNCTION fn_documents_enforce_computation_user_lineage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    computation_owner UUID;
BEGIN
    IF NEW.computation_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT user_id INTO computation_owner
    FROM computations
    WHERE id = NEW.computation_id;

    IF computation_owner IS NULL THEN
        RAISE EXCEPTION 'documents lineage requires existing computation';
    END IF;
    IF computation_owner <> NEW.user_id THEN
        RAISE EXCEPTION 'documents lineage user mismatch';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_documents_enforce_computation_user_lineage ON documents;
CREATE TRIGGER trg_documents_enforce_computation_user_lineage
    BEFORE INSERT OR UPDATE OF user_id, computation_id ON documents
    FOR EACH ROW
    EXECUTE FUNCTION fn_documents_enforce_computation_user_lineage();

CREATE OR REPLACE FUNCTION fn_forms_enforce_computation_user_lineage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    computation_owner UUID;
BEGIN
    SELECT user_id INTO computation_owner
    FROM computations
    WHERE id = NEW.computation_id;

    IF computation_owner IS NULL THEN
        RAISE EXCEPTION 'forms lineage requires existing computation';
    END IF;
    IF computation_owner <> NEW.user_id THEN
        RAISE EXCEPTION 'forms lineage user mismatch';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_forms_enforce_computation_user_lineage ON forms;
CREATE TRIGGER trg_forms_enforce_computation_user_lineage
    BEFORE INSERT OR UPDATE OF user_id, computation_id ON forms
    FOR EACH ROW
    EXECUTE FUNCTION fn_forms_enforce_computation_user_lineage();

CREATE OR REPLACE FUNCTION fn_reports_enforce_computation_user_lineage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    computation_owner UUID;
BEGIN
    SELECT user_id INTO computation_owner
    FROM computations
    WHERE id = NEW.computation_id;

    IF computation_owner IS NULL THEN
        RAISE EXCEPTION 'reports lineage requires existing computation';
    END IF;
    IF computation_owner <> NEW.user_id THEN
        RAISE EXCEPTION 'reports lineage user mismatch';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_reports_enforce_computation_user_lineage ON reports;
CREATE TRIGGER trg_reports_enforce_computation_user_lineage
    BEFORE INSERT OR UPDATE OF user_id, computation_id ON reports
    FOR EACH ROW
    EXECUTE FUNCTION fn_reports_enforce_computation_user_lineage();

CREATE OR REPLACE FUNCTION fn_submissions_enforce_user_lineage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    computation_owner UUID;
    confirmation_owner UUID;
BEGIN
    SELECT user_id INTO computation_owner
    FROM computations
    WHERE id = NEW.computation_id;

    IF computation_owner IS NULL THEN
        RAISE EXCEPTION 'submissions lineage requires existing computation';
    END IF;
    IF computation_owner <> NEW.user_id THEN
        RAISE EXCEPTION 'submissions computation user mismatch';
    END IF;

    SELECT user_id INTO confirmation_owner
    FROM audit_events
    WHERE id = NEW.confirmation_event_id;

    IF confirmation_owner IS NULL THEN
        RAISE EXCEPTION 'submissions lineage requires existing confirmation event';
    END IF;
    IF confirmation_owner <> NEW.user_id THEN
        RAISE EXCEPTION 'submissions confirmation event user mismatch';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_submissions_enforce_user_lineage ON submissions;
CREATE TRIGGER trg_submissions_enforce_user_lineage
    BEFORE INSERT OR UPDATE OF user_id, computation_id, confirmation_event_id ON submissions
    FOR EACH ROW
    EXECUTE FUNCTION fn_submissions_enforce_user_lineage();

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
