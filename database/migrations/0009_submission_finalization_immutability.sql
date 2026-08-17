BEGIN;

CREATE OR REPLACE FUNCTION fn_submissions_enforce_finalization_immutability()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status = 'confirmed' THEN
        IF NEW.form_id IS DISTINCT FROM OLD.form_id THEN
            RAISE EXCEPTION 'confirmed submission form_id is immutable';
        END IF;
        IF NEW.report_id IS DISTINCT FROM OLD.report_id THEN
            RAISE EXCEPTION 'confirmed submission report_id is immutable';
        END IF;
        IF NEW.computation_id IS DISTINCT FROM OLD.computation_id THEN
            RAISE EXCEPTION 'confirmed submission computation_id is immutable';
        END IF;
        IF NEW.confirmation_event_id IS DISTINCT FROM OLD.confirmation_event_id THEN
            RAISE EXCEPTION 'confirmed submission confirmation_event_id is immutable';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_submissions_enforce_finalization_immutability ON submissions;
CREATE TRIGGER trg_submissions_enforce_finalization_immutability
    BEFORE UPDATE ON submissions
    FOR EACH ROW
    EXECUTE FUNCTION fn_submissions_enforce_finalization_immutability();

COMMIT;
