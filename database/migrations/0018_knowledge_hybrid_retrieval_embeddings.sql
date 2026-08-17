BEGIN;

CREATE TABLE IF NOT EXISTS knowledge_chunk_embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id UUID NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimensions INTEGER NOT NULL,
    embedding_vector_json JSONB NOT NULL,
    content_checksum_sha256 TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_knowledge_chunk_embeddings_chunk_id_knowledge_chunks
        FOREIGN KEY (chunk_id) REFERENCES knowledge_chunks (id) ON DELETE RESTRICT,
    CONSTRAINT uq_knowledge_chunk_embeddings_chunk_id_embedding_model UNIQUE (
        chunk_id,
        embedding_model
    ),
    CONSTRAINT chk_knowledge_chunk_embeddings_embedding_dimensions CHECK (
        embedding_dimensions > 0
    ),
    CONSTRAINT chk_knowledge_chunk_embeddings_embedding_vector_json_is_array CHECK (
        jsonb_typeof(embedding_vector_json) = 'array'
    ),
    CONSTRAINT chk_knowledge_chunk_embeddings_content_checksum_not_blank CHECK (
        char_length(btrim(content_checksum_sha256)) > 0
    )
);

CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_embeddings_chunk_id_embedding_model
    ON knowledge_chunk_embeddings (chunk_id, embedding_model);

CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_embeddings_embedding_model_created_at
    ON knowledge_chunk_embeddings (embedding_model, created_at);

CREATE OR REPLACE FUNCTION fn_knowledge_chunk_embeddings_enforce_searchable_parent()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    parent_publication_state TEXT;
    parent_source_input_origin TEXT;
    parent_publication_event_id UUID;
    parent_source_input_ref TEXT;
BEGIN
    SELECT
        ksv.publication_state,
        ksv.source_input_origin,
        ksv.publication_event_id,
        ksv.source_input_ref
    INTO
        parent_publication_state,
        parent_source_input_origin,
        parent_publication_event_id,
        parent_source_input_ref
    FROM knowledge_chunks AS kc
    JOIN knowledge_anchors AS ka
      ON ka.anchor_id = kc.anchor_id
    JOIN knowledge_source_versions AS ksv
      ON ksv.id = ka.source_version_id
    WHERE kc.id = NEW.chunk_id;

    IF parent_publication_state IS NULL THEN
        RAISE EXCEPTION 'knowledge_chunk_embeddings requires existing chunk lineage';
    END IF;

    IF parent_publication_state NOT IN ('published', 'superseded')
       OR parent_source_input_origin NOT IN ('official_source_upload', 'official_source_url')
       OR parent_publication_event_id IS NULL
       OR char_length(btrim(parent_source_input_ref)) = 0 THEN
        RAISE EXCEPTION 'knowledge_chunk_embeddings parent chunk must inherit searchable governed lineage';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_knowledge_chunk_embeddings_enforce_searchable_parent
    ON knowledge_chunk_embeddings;

CREATE TRIGGER trg_knowledge_chunk_embeddings_enforce_searchable_parent
    BEFORE INSERT OR UPDATE ON knowledge_chunk_embeddings
    FOR EACH ROW
    EXECUTE FUNCTION fn_knowledge_chunk_embeddings_enforce_searchable_parent();

CREATE OR REPLACE FUNCTION fn_knowledge_chunk_embeddings_prevent_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'knowledge_chunk_embeddings are immutable after creation';
END;
$$;

DROP TRIGGER IF EXISTS trg_knowledge_chunk_embeddings_prevent_update
    ON knowledge_chunk_embeddings;

CREATE TRIGGER trg_knowledge_chunk_embeddings_prevent_update
    BEFORE UPDATE ON knowledge_chunk_embeddings
    FOR EACH ROW
    EXECUTE FUNCTION fn_knowledge_chunk_embeddings_prevent_mutation();

DROP TRIGGER IF EXISTS trg_knowledge_chunk_embeddings_prevent_delete
    ON knowledge_chunk_embeddings;

CREATE TRIGGER trg_knowledge_chunk_embeddings_prevent_delete
    BEFORE DELETE ON knowledge_chunk_embeddings
    FOR EACH ROW
    EXECUTE FUNCTION fn_knowledge_chunk_embeddings_prevent_mutation();

COMMIT;
