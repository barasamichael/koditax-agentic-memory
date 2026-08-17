-- Document AI CockroachDB native vector storage and distributed indexing.
--
-- This migration tightens the chunk-embedding dimension invariant to the
-- current 1536-dimensional embedding contract and creates the native
-- CockroachDB distributed vector index used for tenant-scoped semantic
-- retrieval.

ALTER TABLE document_ai_chunk_embeddings
    DROP CONSTRAINT IF EXISTS chk_document_ai_chunk_embeddings_dimensions;

ALTER TABLE document_ai_chunk_embeddings
    ADD CONSTRAINT chk_document_ai_chunk_embeddings_dimensions
    CHECK (embedding_dimensions = 1536);

CREATE VECTOR INDEX IF NOT EXISTS idx_document_ai_chunk_embeddings_vector_search
    ON document_ai_chunk_embeddings (
        tenant_id,
        embedding_model,
        embedding_version,
        index_state,
        embedding
    );
