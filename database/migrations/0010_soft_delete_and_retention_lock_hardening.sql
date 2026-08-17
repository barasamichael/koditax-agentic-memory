BEGIN;

CREATE OR REPLACE FUNCTION fn_computations_prevent_illegal_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM submissions
        WHERE computation_id = OLD.id
    ) THEN
        RAISE EXCEPTION 'cannot delete computation with submissions';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM documents
        WHERE computation_id = OLD.id
          AND (state <> 'purged' OR purged_at IS NULL)
    ) THEN
        RAISE EXCEPTION 'cannot delete computation with non-purged documents';
    END IF;

    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_computations_prevent_illegal_delete ON computations;
CREATE TRIGGER trg_computations_prevent_illegal_delete
    BEFORE DELETE ON computations
    FOR EACH ROW
    EXECUTE FUNCTION fn_computations_prevent_illegal_delete();

CREATE OR REPLACE FUNCTION fn_users_prevent_illegal_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM submissions
        WHERE user_id = OLD.id
    ) THEN
        RAISE EXCEPTION 'cannot delete user with submissions';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM audit_events
        WHERE user_id = OLD.id
    ) THEN
        RAISE EXCEPTION 'cannot delete user with audit events';
    END IF;

    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_users_prevent_illegal_delete ON users;
CREATE TRIGGER trg_users_prevent_illegal_delete
    BEFORE DELETE ON users
    FOR EACH ROW
    EXECUTE FUNCTION fn_users_prevent_illegal_delete();

CREATE OR REPLACE FUNCTION fn_computations_enforce_retention_lock()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.compliance_lock_until IS NOT NULL
       AND OLD.compliance_lock_until > now() THEN
        RAISE EXCEPTION 'computation is retention-locked';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        RETURN NEW;
    END IF;

    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_computations_enforce_retention_lock ON computations;
CREATE TRIGGER trg_computations_enforce_retention_lock
    BEFORE UPDATE OR DELETE ON computations
    FOR EACH ROW
    EXECUTE FUNCTION fn_computations_enforce_retention_lock();

CREATE OR REPLACE FUNCTION fn_documents_enforce_retention_lock()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.compliance_lock_until IS NOT NULL
       AND OLD.compliance_lock_until > now() THEN
        RAISE EXCEPTION 'document is retention-locked';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        RETURN NEW;
    END IF;

    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_documents_enforce_retention_lock ON documents;
CREATE TRIGGER trg_documents_enforce_retention_lock
    BEFORE UPDATE OR DELETE ON documents
    FOR EACH ROW
    EXECUTE FUNCTION fn_documents_enforce_retention_lock();

COMMIT;
