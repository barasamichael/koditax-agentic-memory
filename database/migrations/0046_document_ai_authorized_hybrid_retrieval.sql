-- Milestone 20 — bounded semantic discovery is always resolved to canonical elements.
BEGIN;

CREATE INDEX IF NOT EXISTS idx_document_ai_chunk_embeddings_cosine_active
    ON document_ai_chunk_embeddings USING hnsw (embedding vector_cosine_ops)
    WHERE index_state = 'active';

CREATE INDEX IF NOT EXISTS idx_document_ai_retrieval_chunks_active_canonical_scope
    ON document_ai_retrieval_chunks
       (tenant_id, document_id, document_version_id, canonical_representation_id)
    WHERE lifecycle_state = 'active';

COMMIT;
