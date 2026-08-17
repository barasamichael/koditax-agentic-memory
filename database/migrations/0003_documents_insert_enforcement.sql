BEGIN;

DROP TRIGGER IF EXISTS trg_documents_enforce_state_transition ON documents;
CREATE TRIGGER trg_documents_enforce_state_transition
    BEFORE INSERT OR UPDATE OF state, purged_at, purge_eligible_at ON documents
    FOR EACH ROW
    EXECUTE FUNCTION fn_documents_enforce_state_transition();

COMMIT;
