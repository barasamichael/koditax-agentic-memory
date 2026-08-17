-- Document AI CockroachDB exact retrieval indexes.
--
-- Exact retrieval uses active retrieval chunks and structured chunk metadata.
-- The lexical path is accelerated with a trigram index over chunk text, while
-- the structural path uses inverted indexes on the persisted JSON payloads.

CREATE INDEX IF NOT EXISTS idx_document_ai_retrieval_chunks_exact_lexical
    ON document_ai_retrieval_chunks USING GIN (embedding_text gin_trgm_ops)
    WHERE lifecycle_state = 'active';

CREATE INVERTED INDEX IF NOT EXISTS idx_document_ai_retrieval_chunks_exact_source_location
    ON document_ai_retrieval_chunks (source_location)
    WHERE lifecycle_state = 'active';

CREATE INVERTED INDEX IF NOT EXISTS idx_document_ai_retrieval_chunks_exact_structural_context
    ON document_ai_retrieval_chunks (structural_context)
    WHERE lifecycle_state = 'active';
