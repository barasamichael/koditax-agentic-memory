BEGIN;

CREATE OR REPLACE FUNCTION fn_computations_enforce_session_user_lineage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    session_owner UUID;
BEGIN
    IF NEW.session_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT user_id INTO session_owner
    FROM sessions
    WHERE id = NEW.session_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'computations.session_id references missing session';
    END IF;

    IF session_owner IS DISTINCT FROM NEW.user_id THEN
        RAISE EXCEPTION 'computations session lineage user mismatch';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_computations_enforce_session_user_lineage ON computations;
CREATE TRIGGER trg_computations_enforce_session_user_lineage
    BEFORE INSERT OR UPDATE OF user_id, session_id ON computations
    FOR EACH ROW
    EXECUTE FUNCTION fn_computations_enforce_session_user_lineage();

COMMIT;
