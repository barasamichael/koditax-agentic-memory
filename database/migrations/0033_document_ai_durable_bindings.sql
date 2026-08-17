-- Milestone 7 / FR-003: durable, tenant-safe conversation and workflow bindings.
BEGIN;

ALTER TABLE document_ai_document_bindings
    ADD COLUMN IF NOT EXISTS binding_role TEXT NULL,
    ADD COLUMN IF NOT EXISTS conversation_id TEXT NULL,
    ADD COLUMN IF NOT EXISTS turn_id TEXT NULL,
    ADD COLUMN IF NOT EXISTS workflow_id TEXT NULL,
    ADD COLUMN IF NOT EXISTS attachment_order INTEGER NULL,
    ADD COLUMN IF NOT EXISTS correlation_id TEXT NULL,
    ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ NULL;

ALTER TABLE document_ai_document_bindings
    ADD CONSTRAINT chk_document_ai_document_bindings_role CHECK (
        binding_role IS NULL OR binding_role IN (
            'conversation_attachment', 'current_turn_attachment',
            'existing_library_document', 'workflow_reference'
        )
    ),
    ADD CONSTRAINT chk_document_ai_document_bindings_target CHECK (
        (conversation_id IS NOT NULL AND workflow_id IS NULL)
        OR (conversation_id IS NULL AND workflow_id IS NOT NULL)
        OR (conversation_id IS NULL AND workflow_id IS NULL)
    ),
    ADD CONSTRAINT chk_document_ai_document_bindings_turn CHECK (
        turn_id IS NULL OR conversation_id IS NOT NULL
    ),
    ADD CONSTRAINT chk_document_ai_document_bindings_attachment_order CHECK (
        attachment_order IS NULL OR attachment_order >= 0
    );

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_document_bindings_logical_target
    ON document_ai_document_bindings (
        tenant_id, document_id, COALESCE(document_version_id, '00000000-0000-0000-0000-000000000000'::uuid),
        binding_role, COALESCE(conversation_id, ''), COALESCE(turn_id, ''), COALESCE(workflow_id, '')
    ) WHERE revoked_at IS NULL AND binding_role IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_document_ai_document_bindings_conversation
    ON document_ai_document_bindings (tenant_id, conversation_id, turn_id, attachment_order, bound_at)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_document_ai_document_bindings_workflow
    ON document_ai_document_bindings (tenant_id, workflow_id, bound_at)
    WHERE revoked_at IS NULL;

CREATE OR REPLACE FUNCTION fn_document_ai_binding_version_same_document()
RETURNS TRIGGER AS $$
DECLARE
    version_document_id UUID;
BEGIN
    IF NEW.document_version_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT document_id INTO version_document_id
    FROM document_ai_document_versions
    WHERE tenant_id = NEW.tenant_id AND document_version_id = NEW.document_version_id;
    IF version_document_id IS DISTINCT FROM NEW.document_id THEN
        RAISE EXCEPTION 'bound version must belong to the same document and tenant';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_document_ai_binding_version_same_document ON document_ai_document_bindings;
CREATE TRIGGER trg_document_ai_binding_version_same_document
    BEFORE INSERT OR UPDATE OF tenant_id, document_id, document_version_id
    ON document_ai_document_bindings
    FOR EACH ROW EXECUTE FUNCTION fn_document_ai_binding_version_same_document();

COMMIT;
