BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_delegations_active_revoked_consistency'
          AND conrelid = 'delegations'::regclass
    ) THEN
        ALTER TABLE delegations
            ADD CONSTRAINT chk_delegations_active_revoked_consistency
            CHECK (is_active = (revoked_at IS NULL));
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = 'delegations'
          AND indexdef ILIKE 'CREATE UNIQUE INDEX%'
          AND indexdef LIKE '%(principal_user_id, delegate_user_id)%'
          AND indexdef LIKE '%WHERE is_active%'
    ) THEN
        CREATE UNIQUE INDEX ux_delegations_one_active_per_pair
            ON delegations (principal_user_id, delegate_user_id)
            WHERE is_active = TRUE;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION fn_delegations_prevent_reactivation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.revoked_at IS NOT NULL THEN
            IF NEW.is_active IS TRUE THEN
                RAISE EXCEPTION 'revoked delegation cannot be reactivated; create a new row';
            END IF;
            IF NEW.revoked_at IS NULL THEN
                RAISE EXCEPTION 'revoked_at cannot be cleared; create a new row';
            END IF;
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_delegations_prevent_reactivation ON delegations;
CREATE TRIGGER trg_delegations_prevent_reactivation
    BEFORE UPDATE ON delegations
    FOR EACH ROW
    EXECUTE FUNCTION fn_delegations_prevent_reactivation();

COMMIT;
