CREATE TABLE IF NOT EXISTS document_ai_extraction_calculations (
    calculation_id TEXT PRIMARY KEY,
    extraction_id UUID NOT NULL REFERENCES document_ai_extractions (extraction_id) ON DELETE RESTRICT,
    document_id UUID NOT NULL REFERENCES document_ai_documents (document_id) ON DELETE RESTRICT,
    calculation_kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_document_ai_extraction_calculations_extraction
    ON document_ai_extraction_calculations (extraction_id, created_at DESC);
