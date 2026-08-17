BEGIN;

ALTER TABLE computation_results
    ADD COLUMN IF NOT EXISTS user_id UUID;

UPDATE computation_results AS computation_results_row
SET user_id = computations_row.user_id
FROM computations AS computations_row
WHERE computations_row.id = computation_results_row.computation_id
  AND computation_results_row.user_id IS NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'computation_results'
          AND column_name = 'user_id'
          AND is_nullable = 'YES'
    ) THEN
        ALTER TABLE computation_results
            ALTER COLUMN user_id SET NOT NULL;
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_computation_results_user_id_users'
          AND conrelid = 'computation_results'::regclass
    ) THEN
        ALTER TABLE computation_results
            ADD CONSTRAINT fk_computation_results_user_id_users
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION fn_computation_results_enforce_computation_user_lineage()
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
        RAISE EXCEPTION 'computation_results lineage requires existing computation';
    END IF;
    IF computation_owner <> NEW.user_id THEN
        RAISE EXCEPTION 'computation_results lineage user mismatch';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_computation_results_enforce_computation_user_lineage ON computation_results;
CREATE TRIGGER trg_computation_results_enforce_computation_user_lineage
    BEFORE INSERT OR UPDATE OF user_id, computation_id ON computation_results
    FOR EACH ROW
    EXECUTE FUNCTION fn_computation_results_enforce_computation_user_lineage();

ALTER TABLE validations
    ADD COLUMN IF NOT EXISTS user_id UUID;

UPDATE validations AS validations_row
SET user_id = computations_row.user_id
FROM computations AS computations_row
WHERE computations_row.id = validations_row.computation_id
  AND validations_row.user_id IS NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'validations'
          AND column_name = 'user_id'
          AND is_nullable = 'YES'
    ) THEN
        ALTER TABLE validations
            ALTER COLUMN user_id SET NOT NULL;
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_validations_user_id_users'
          AND conrelid = 'validations'::regclass
    ) THEN
        ALTER TABLE validations
            ADD CONSTRAINT fk_validations_user_id_users
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION fn_validations_enforce_computation_user_lineage()
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
        RAISE EXCEPTION 'validations lineage requires existing computation';
    END IF;
    IF computation_owner <> NEW.user_id THEN
        RAISE EXCEPTION 'validations lineage user mismatch';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validations_enforce_computation_user_lineage ON validations;
CREATE TRIGGER trg_validations_enforce_computation_user_lineage
    BEFORE INSERT OR UPDATE OF user_id, computation_id ON validations
    FOR EACH ROW
    EXECUTE FUNCTION fn_validations_enforce_computation_user_lineage();

ALTER TABLE document_extractions
    ADD COLUMN IF NOT EXISTS user_id UUID;

UPDATE document_extractions AS document_extractions_row
SET user_id = documents_row.user_id
FROM documents AS documents_row
WHERE documents_row.id = document_extractions_row.document_id
  AND document_extractions_row.user_id IS NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'document_extractions'
          AND column_name = 'user_id'
          AND is_nullable = 'YES'
    ) THEN
        ALTER TABLE document_extractions
            ALTER COLUMN user_id SET NOT NULL;
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_document_extractions_user_id_users'
          AND conrelid = 'document_extractions'::regclass
    ) THEN
        ALTER TABLE document_extractions
            ADD CONSTRAINT fk_document_extractions_user_id_users
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION fn_document_extractions_enforce_document_user_lineage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    document_owner UUID;
BEGIN
    SELECT user_id INTO document_owner
    FROM documents
    WHERE id = NEW.document_id;

    IF document_owner IS NULL THEN
        RAISE EXCEPTION 'document_extractions lineage requires existing document';
    END IF;
    IF document_owner <> NEW.user_id THEN
        RAISE EXCEPTION 'document_extractions lineage user mismatch';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_document_extractions_enforce_document_user_lineage ON document_extractions;
CREATE TRIGGER trg_document_extractions_enforce_document_user_lineage
    BEFORE INSERT OR UPDATE OF user_id, document_id ON document_extractions
    FOR EACH ROW
    EXECUTE FUNCTION fn_document_extractions_enforce_document_user_lineage();

CREATE OR REPLACE FUNCTION fn_forms_enforce_document_user_lineage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    document_owner UUID;
BEGIN
    IF NEW.document_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT user_id INTO document_owner
    FROM documents
    WHERE id = NEW.document_id;

    IF document_owner IS NULL THEN
        RAISE EXCEPTION 'forms lineage requires existing document';
    END IF;
    IF document_owner <> NEW.user_id THEN
        RAISE EXCEPTION 'forms document user mismatch';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_forms_enforce_document_user_lineage ON forms;
CREATE TRIGGER trg_forms_enforce_document_user_lineage
    BEFORE INSERT OR UPDATE OF user_id, document_id ON forms
    FOR EACH ROW
    EXECUTE FUNCTION fn_forms_enforce_document_user_lineage();

COMMIT;
