ALTER TABLE document_ai_extraction_jobs
    ADD COLUMN IF NOT EXISTS execution_payload JSONB NULL;
