BEGIN;

ALTER TABLE computations
    ADD COLUMN finalized_at TIMESTAMPTZ,
    ADD COLUMN finalized_audit_event_id UUID;

ALTER TABLE computations
    ADD CONSTRAINT fk_computations_finalized_audit_event_id_audit_events
        FOREIGN KEY (finalized_audit_event_id) REFERENCES audit_events (id) ON DELETE RESTRICT;

ALTER TABLE computations
    ADD CONSTRAINT chk_computations_finalization_pair_consistency
        CHECK ((finalized_at IS NULL) = (finalized_audit_event_id IS NULL));

CREATE OR REPLACE FUNCTION fn_computations_enforce_finalization_immutability()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (NEW.finalized_at IS NULL) <> (NEW.finalized_audit_event_id IS NULL) THEN
        RAISE EXCEPTION 'computations finalization fields must be set together';
    END IF;

    IF OLD.finalized_at IS NULL THEN
        RETURN NEW;
    END IF;

    IF NEW.finalized_at IS NULL THEN
        RAISE EXCEPTION 'computations finalization state cannot be reversed';
    END IF;

    IF NEW.finalized_at IS DISTINCT FROM OLD.finalized_at THEN
        RAISE EXCEPTION 'computations.finalized_at is immutable once finalized';
    END IF;

    IF NEW.finalized_audit_event_id IS DISTINCT FROM OLD.finalized_audit_event_id THEN
        RAISE EXCEPTION 'computations.finalized_audit_event_id is immutable once finalized';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_computations_enforce_finalization_immutability ON computations;
CREATE TRIGGER trg_computations_enforce_finalization_immutability
    BEFORE UPDATE OF finalized_at, finalized_audit_event_id ON computations
    FOR EACH ROW
    EXECUTE FUNCTION fn_computations_enforce_finalization_immutability();

CREATE OR REPLACE FUNCTION fn_computation_results_prevent_mutation_if_finalized()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    computation_is_finalized BOOLEAN;
BEGIN
    SELECT (finalized_at IS NOT NULL) INTO computation_is_finalized
    FROM computations
    WHERE id = OLD.computation_id;

    IF COALESCE(computation_is_finalized, FALSE) THEN
        RAISE EXCEPTION 'cannot mutate computation_results for finalized computation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_computation_results_prevent_mutation_if_finalized ON computation_results;
CREATE TRIGGER trg_computation_results_prevent_mutation_if_finalized
    BEFORE UPDATE OR DELETE ON computation_results
    FOR EACH ROW
    EXECUTE FUNCTION fn_computation_results_prevent_mutation_if_finalized();

CREATE OR REPLACE FUNCTION fn_validations_prevent_mutation_if_finalized()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    computation_is_finalized BOOLEAN;
BEGIN
    SELECT (finalized_at IS NOT NULL) INTO computation_is_finalized
    FROM computations
    WHERE id = OLD.computation_id;

    IF COALESCE(computation_is_finalized, FALSE) THEN
        RAISE EXCEPTION 'cannot mutate validations for finalized computation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validations_prevent_mutation_if_finalized ON validations;
CREATE TRIGGER trg_validations_prevent_mutation_if_finalized
    BEFORE UPDATE OR DELETE ON validations
    FOR EACH ROW
    EXECUTE FUNCTION fn_validations_prevent_mutation_if_finalized();

CREATE OR REPLACE FUNCTION fn_computations_enforce_retention_lock()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.compliance_lock_until IS NOT NULL
       AND OLD.compliance_lock_until > now() THEN
        IF TG_OP = 'UPDATE' THEN
            IF NEW.user_id IS DISTINCT FROM OLD.user_id
               OR NEW.session_id IS DISTINCT FROM OLD.session_id
               OR NEW.tax_type IS DISTINCT FROM OLD.tax_type
               OR NEW.regime_type IS DISTINCT FROM OLD.regime_type
               OR NEW.regime_identifier IS DISTINCT FROM OLD.regime_identifier
               OR NEW.tax_year IS DISTINCT FROM OLD.tax_year
               OR NEW.rule_version IS DISTINCT FROM OLD.rule_version
               OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.correlation_id IS DISTINCT FROM OLD.correlation_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.retention_expires_at IS DISTINCT FROM OLD.retention_expires_at
               OR NEW.compliance_lock_until IS DISTINCT FROM OLD.compliance_lock_until THEN
                RAISE EXCEPTION 'computation is retention-locked';
            END IF;
            RETURN NEW;
        END IF;

        RAISE EXCEPTION 'computation is retention-locked';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        RETURN NEW;
    END IF;

    RETURN OLD;
END;
$$;

COMMIT;
