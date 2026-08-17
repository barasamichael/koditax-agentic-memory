-- Milestone 18 — derived, active-canonical semantic retrieval indexes.
BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- 0029 introduced a narrower retrieval-chunk table before the canonical
-- retrieval lineage was finalized. Upgrade that table in place so a clean
-- migration chain and an existing target database share the same contract.
ALTER TABLE document_ai_retrieval_chunks
    ADD COLUMN IF NOT EXISTS document_id UUID NULL,
    ADD COLUMN IF NOT EXISTS document_version_id UUID NULL,
    ADD COLUMN IF NOT EXISTS chunk_key TEXT NULL,
    ADD COLUMN IF NOT EXISTS content_hash_sha256 TEXT NULL,
    ADD COLUMN IF NOT EXISTS chunking_policy_version TEXT NULL,
    ADD COLUMN IF NOT EXISTS embedding_text TEXT NULL,
    ADD COLUMN IF NOT EXISTS canonical_element_keys JSONB NULL,
    ADD COLUMN IF NOT EXISTS source_location JSONB NULL,
    ADD COLUMN IF NOT EXISTS structural_context JSONB NULL,
    ADD COLUMN IF NOT EXISTS lifecycle_state TEXT NOT NULL DEFAULT 'active';

UPDATE document_ai_retrieval_chunks AS chunk
   SET document_id = version.document_id,
       document_version_id = representation.document_version_id,
       chunk_key = COALESCE(chunk.chunk_key, 'legacy-' || chunk.ordinal::text),
       content_hash_sha256 = COALESCE(chunk.content_hash_sha256, chunk.content_checksum_sha256),
       chunking_policy_version = COALESCE(chunk.chunking_policy_version, chunk.chunking_version),
       embedding_text = COALESCE(chunk.embedding_text, chunk.text_content),
       canonical_element_keys = COALESCE(chunk.canonical_element_keys, '[]'::jsonb),
       source_location = COALESCE(chunk.source_location, '{}'::jsonb),
       structural_context = COALESCE(chunk.structural_context, '{}'::jsonb)
  FROM document_ai_canonical_representations AS representation
  JOIN document_ai_document_versions AS version
    ON version.tenant_id = representation.tenant_id
   AND version.document_version_id = representation.document_version_id
 WHERE representation.tenant_id = chunk.tenant_id
   AND representation.canonical_representation_id = chunk.canonical_representation_id;

ALTER TABLE document_ai_retrieval_chunks
    ALTER COLUMN document_id SET NOT NULL,
    ALTER COLUMN document_version_id SET NOT NULL,
    ALTER COLUMN chunk_key SET NOT NULL,
    ALTER COLUMN content_hash_sha256 SET NOT NULL,
    ALTER COLUMN chunking_policy_version SET NOT NULL,
    ALTER COLUMN embedding_text SET NOT NULL,
    ALTER COLUMN canonical_element_keys SET NOT NULL,
    ALTER COLUMN source_location SET NOT NULL,
    ALTER COLUMN structural_context SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_document_ai_retrieval_chunks_document_scope') THEN
        ALTER TABLE document_ai_retrieval_chunks ADD CONSTRAINT fk_document_ai_retrieval_chunks_document_scope
            FOREIGN KEY (tenant_id, document_id)
            REFERENCES document_ai_documents (tenant_id, document_id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_document_ai_retrieval_chunks_version_scope') THEN
        ALTER TABLE document_ai_retrieval_chunks ADD CONSTRAINT fk_document_ai_retrieval_chunks_version_scope
            FOREIGN KEY (tenant_id, document_version_id)
            REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_document_ai_retrieval_chunks_identity') THEN
        ALTER TABLE document_ai_retrieval_chunks ADD CONSTRAINT uq_document_ai_retrieval_chunks_identity
            UNIQUE (tenant_id, canonical_representation_id, chunk_key, chunking_policy_version);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS document_ai_retrieval_chunks (
    retrieval_chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    canonical_representation_id UUID NOT NULL,
    chunk_key TEXT NOT NULL,
    content_hash_sha256 TEXT NOT NULL,
    chunking_policy_version TEXT NOT NULL,
    embedding_text TEXT NOT NULL,
    canonical_element_keys JSONB NOT NULL,
    source_location JSONB NOT NULL,
    structural_context JSONB NOT NULL,
    lifecycle_state TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_document_ai_retrieval_chunks_document_scope
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES document_ai_documents (tenant_id, document_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_retrieval_chunks_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_retrieval_chunks_representation_scope
        FOREIGN KEY (tenant_id, canonical_representation_id)
        REFERENCES document_ai_canonical_representations (tenant_id, canonical_representation_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_retrieval_chunks_scope
        UNIQUE (tenant_id, retrieval_chunk_id),
    CONSTRAINT uq_document_ai_retrieval_chunks_identity
        UNIQUE (tenant_id, canonical_representation_id, chunk_key, chunking_policy_version),
    CONSTRAINT chk_document_ai_retrieval_chunks_hash
        CHECK (content_hash_sha256 ~ '^[a-f0-9]{64}$'),
    CONSTRAINT chk_document_ai_retrieval_chunks_lifecycle
        CHECK (lifecycle_state IN ('active', 'trashed', 'purge_pending', 'purged'))
);

CREATE TABLE IF NOT EXISTS document_ai_chunk_embeddings (
    chunk_embedding_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    retrieval_chunk_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    canonical_representation_id UUID NOT NULL,
    content_hash_sha256 TEXT NOT NULL,
    chunking_policy_version TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    embedding_dimensions INTEGER NOT NULL,
    embedding VECTOR(1536) NOT NULL,
    index_state TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_document_ai_chunk_embeddings_chunk_scope
        FOREIGN KEY (tenant_id, retrieval_chunk_id)
        REFERENCES document_ai_retrieval_chunks (tenant_id, retrieval_chunk_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_chunk_embeddings_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_chunk_embeddings_representation_scope
        FOREIGN KEY (tenant_id, canonical_representation_id)
        REFERENCES document_ai_canonical_representations (tenant_id, canonical_representation_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_chunk_embeddings_model
        UNIQUE (tenant_id, retrieval_chunk_id, embedding_model, embedding_version),
    CONSTRAINT chk_document_ai_chunk_embeddings_dimensions CHECK (embedding_dimensions > 0),
    CONSTRAINT chk_document_ai_chunk_embeddings_state
        CHECK (index_state IN ('active', 'superseded', 'trashed', 'purged'))
);

CREATE INDEX IF NOT EXISTS idx_document_ai_retrieval_chunks_scope
    ON document_ai_retrieval_chunks
       (tenant_id, document_id, document_version_id, lifecycle_state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_document_ai_chunk_embeddings_scope
    ON document_ai_chunk_embeddings
       (tenant_id, document_version_id, canonical_representation_id, embedding_model,
        embedding_version, index_state);

-- No candidate representation may acquire a retrieval vector.  Document lifecycle
-- transitions must mark derived chunks and vectors ineligible before retrieval.
CREATE OR REPLACE FUNCTION fn_document_ai_retrieval_chunk_active_authority()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM document_ai_canonical_representations representation
         WHERE representation.tenant_id = NEW.tenant_id
           AND representation.canonical_representation_id = NEW.canonical_representation_id
           AND representation.document_version_id = NEW.document_version_id
           AND representation.is_active AND representation.state = 'active'
    ) THEN
        RAISE EXCEPTION 'retrieval chunks require active canonical authority';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_document_ai_retrieval_chunk_active_authority
    ON document_ai_retrieval_chunks;
CREATE TRIGGER trg_document_ai_retrieval_chunk_active_authority
BEFORE INSERT OR UPDATE ON document_ai_retrieval_chunks
FOR EACH ROW EXECUTE FUNCTION fn_document_ai_retrieval_chunk_active_authority();

CREATE OR REPLACE FUNCTION fn_document_ai_embedding_representation_scope()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.is_active AND NOT NEW.is_active THEN
        UPDATE document_ai_chunk_embeddings
           SET index_state = 'superseded'
         WHERE tenant_id = OLD.tenant_id
           AND canonical_representation_id = OLD.canonical_representation_id
           AND index_state = 'active';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_document_ai_embedding_representation_scope
    ON document_ai_canonical_representations;
CREATE TRIGGER trg_document_ai_embedding_representation_scope
AFTER UPDATE OF is_active ON document_ai_canonical_representations
FOR EACH ROW EXECUTE FUNCTION fn_document_ai_embedding_representation_scope();

CREATE OR REPLACE FUNCTION fn_document_ai_retrieval_lifecycle_scope()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state IN ('trashed', 'purge_pending', 'eligible_for_purge', 'purged')
       AND NEW.state IS DISTINCT FROM OLD.state THEN
        UPDATE document_ai_retrieval_chunks AS chunk
           SET lifecycle_state = CASE WHEN NEW.state = 'purged' THEN 'purged'
                                      WHEN NEW.state = 'trashed' THEN 'trashed'
                                      ELSE 'purge_pending' END
         WHERE chunk.tenant_id = NEW.tenant_id AND chunk.document_id = NEW.document_id;
        UPDATE document_ai_chunk_embeddings AS embedding
           SET index_state = CASE WHEN NEW.state = 'purged' THEN 'purged' ELSE 'trashed' END
          FROM document_ai_retrieval_chunks AS chunk
         WHERE chunk.tenant_id = embedding.tenant_id
           AND chunk.retrieval_chunk_id = embedding.retrieval_chunk_id
           AND chunk.tenant_id = NEW.tenant_id AND chunk.document_id = NEW.document_id;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_document_ai_retrieval_lifecycle_scope ON document_ai_documents;
CREATE TRIGGER trg_document_ai_retrieval_lifecycle_scope
AFTER UPDATE OF state ON document_ai_documents
FOR EACH ROW EXECUTE FUNCTION fn_document_ai_retrieval_lifecycle_scope();

COMMIT;
