BEGIN;

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE IF NOT EXISTS knowledge_sources (
    source_id TEXT PRIMARY KEY,
    source_family_id TEXT NOT NULL,
    title TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    source_class TEXT NOT NULL,
    authority_level TEXT NOT NULL,
    tax_domain TEXT NOT NULL,
    issuing_authority TEXT NOT NULL,
    jurisdiction TEXT NOT NULL DEFAULT 'KE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by UUID NOT NULL,
    retired_at TIMESTAMPTZ,
    CONSTRAINT fk_knowledge_sources_created_by_users
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_knowledge_sources_source_family_id UNIQUE (source_family_id),
    CONSTRAINT chk_knowledge_sources_source_class CHECK (
        source_class IN ('tax_law', 'regulation', 'guidance', 'commentary')
    ),
    CONSTRAINT chk_knowledge_sources_authority_level CHECK (
        authority_level IN ('statute', 'regulation', 'guidance', 'commentary')
    ),
    CONSTRAINT chk_knowledge_sources_authority_source_class_binding CHECK (
        (source_class = 'tax_law' AND authority_level = 'statute')
        OR (source_class = 'regulation' AND authority_level = 'regulation')
        OR (source_class = 'guidance' AND authority_level = 'guidance')
        OR (source_class = 'commentary' AND authority_level = 'commentary')
    ),
    CONSTRAINT chk_knowledge_sources_jurisdiction CHECK (jurisdiction = 'KE'),
    CONSTRAINT chk_knowledge_sources_retired_after_create CHECK (
        retired_at IS NULL OR retired_at >= created_at
    )
);

CREATE TABLE IF NOT EXISTS knowledge_source_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id TEXT NOT NULL,
    document_id UUID,
    point_in_time_url TEXT NOT NULL,
    source_checksum_sha256 TEXT NOT NULL,
    source_version_form TEXT NOT NULL,
    source_input_origin TEXT NOT NULL,
    source_input_ref TEXT NOT NULL,
    publication_state TEXT NOT NULL,
    effective_from DATE NOT NULL,
    effective_to DATE,
    tax_year INTEGER,
    supersedes_source_version_id UUID,
    publication_event_id UUID,
    approved_at TIMESTAMPTZ,
    approved_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_knowledge_source_versions_source_id_knowledge_sources
        FOREIGN KEY (source_id) REFERENCES knowledge_sources (source_id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_source_versions_document_id_documents
        FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_source_versions_supersedes_source_version_id_knowledge_source_versions
        FOREIGN KEY (supersedes_source_version_id)
        REFERENCES knowledge_source_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_source_versions_publication_event_id_audit_events
        FOREIGN KEY (publication_event_id) REFERENCES audit_events (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_source_versions_approved_by_users
        FOREIGN KEY (approved_by) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT chk_knowledge_source_versions_source_version_form CHECK (
        source_version_form IN ('as_issued', 'point_in_time_consolidation')
    ),
    CONSTRAINT chk_knowledge_source_versions_source_input_origin CHECK (
        source_input_origin IN ('official_source_upload', 'official_source_url')
    ),
    CONSTRAINT chk_knowledge_source_versions_publication_state CHECK (
        publication_state IN (
            'draft',
            'review_pending',
            'approved',
            'published',
            'superseded',
            'archived',
            'rejected'
        )
    ),
    CONSTRAINT chk_knowledge_source_versions_effective_window CHECK (
        effective_to IS NULL OR effective_from <= effective_to
    ),
    CONSTRAINT chk_knowledge_source_versions_tax_year_bounds CHECK (
        tax_year IS NULL OR tax_year BETWEEN 1900 AND 2100
    ),
    CONSTRAINT chk_knowledge_source_versions_source_input_ref_not_blank CHECK (
        char_length(btrim(source_input_ref)) > 0
    ),
    CONSTRAINT chk_knowledge_source_versions_official_upload_requires_document_id CHECK (
        source_input_origin <> 'official_source_upload' OR document_id IS NOT NULL
    ),
    CONSTRAINT chk_knowledge_source_versions_searchable_lineage_required CHECK (
        publication_state NOT IN ('published', 'superseded')
        OR (
            publication_event_id IS NOT NULL
            AND char_length(btrim(source_input_ref)) > 0
            AND source_input_origin IN ('official_source_upload', 'official_source_url')
        )
    ),
    CONSTRAINT chk_knowledge_source_versions_no_self_supersession CHECK (
        supersedes_source_version_id IS NULL OR supersedes_source_version_id <> id
    ),
    CONSTRAINT ex_knowledge_source_versions_effective_window_no_overlap EXCLUDE USING gist (
        source_id WITH =,
        daterange(
            effective_from,
            COALESCE(effective_to + 1, 'infinity'::date),
            '[)'
        ) WITH &&
    )
);

CREATE TABLE IF NOT EXISTS knowledge_anchors (
    anchor_id TEXT PRIMARY KEY,
    source_version_id UUID NOT NULL,
    anchor_title TEXT NOT NULL,
    anchor_path TEXT NOT NULL,
    anchor_text TEXT NOT NULL,
    normalized_anchor_text TEXT NOT NULL,
    temporal_scope_from DATE NOT NULL,
    temporal_scope_to DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_knowledge_anchors_source_version_id_knowledge_source_versions
        FOREIGN KEY (source_version_id)
        REFERENCES knowledge_source_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_knowledge_anchors_source_version_id_anchor_path UNIQUE (
        source_version_id,
        anchor_path
    ),
    CONSTRAINT chk_knowledge_anchors_anchor_path_not_blank CHECK (
        char_length(btrim(anchor_path)) > 0
    ),
    CONSTRAINT chk_knowledge_anchors_temporal_scope CHECK (
        temporal_scope_to IS NULL OR temporal_scope_from <= temporal_scope_to
    )
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    anchor_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    normalized_chunk_text TEXT NOT NULL,
    embedding_vector_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_knowledge_chunks_anchor_id_knowledge_anchors
        FOREIGN KEY (anchor_id) REFERENCES knowledge_anchors (anchor_id) ON DELETE RESTRICT,
    CONSTRAINT uq_knowledge_chunks_anchor_id_chunk_index UNIQUE (anchor_id, chunk_index),
    CONSTRAINT chk_knowledge_chunks_chunk_index CHECK (chunk_index >= 0)
);

CREATE TABLE IF NOT EXISTS knowledge_ingestion_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL,
    requested_by UUID NOT NULL,
    ingestion_state TEXT NOT NULL,
    extracted_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    proposed_source_record JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_notes JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT fk_knowledge_ingestion_jobs_document_id_documents
        FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_ingestion_jobs_requested_by_users
        FOREIGN KEY (requested_by) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT chk_knowledge_ingestion_jobs_state CHECK (
        ingestion_state IN (
            'uploaded',
            'parsed',
            'review_pending',
            'approved_for_publication',
            'published',
            'rejected',
            'archived'
        )
    ),
    CONSTRAINT chk_knowledge_ingestion_jobs_completed_at_order CHECK (
        completed_at IS NULL OR completed_at >= created_at
    )
);

CREATE INDEX IF NOT EXISTS idx_knowledge_sources_tax_domain_authority_source_class
    ON knowledge_sources (tax_domain, authority_level, source_class);

CREATE INDEX IF NOT EXISTS idx_knowledge_source_versions_source_id_publication_effective_window
    ON knowledge_source_versions (source_id, publication_state, effective_from, effective_to);

CREATE INDEX IF NOT EXISTS idx_knowledge_source_versions_searchable_effective_window
    ON knowledge_source_versions (publication_state, effective_from, effective_to)
    WHERE publication_state IN ('published', 'superseded');

CREATE INDEX IF NOT EXISTS idx_knowledge_source_versions_tax_year
    ON knowledge_source_versions (tax_year);

CREATE INDEX IF NOT EXISTS idx_knowledge_anchors_source_version_id_temporal_scope
    ON knowledge_anchors (source_version_id, temporal_scope_from, temporal_scope_to);

CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_anchor_id_chunk_index
    ON knowledge_chunks (anchor_id, chunk_index);

CREATE INDEX IF NOT EXISTS idx_knowledge_ingestion_jobs_state_created_at
    ON knowledge_ingestion_jobs (ingestion_state, created_at);

CREATE OR REPLACE FUNCTION fn_knowledge_source_versions_enforce_governed_rules()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    parent_source_class TEXT;
    superseded_source_id TEXT;
BEGIN
    SELECT source_class
    INTO parent_source_class
    FROM knowledge_sources
    WHERE source_id = NEW.source_id;

    IF parent_source_class IS NULL THEN
        RAISE EXCEPTION 'knowledge_source_versions requires existing source_id lineage';
    END IF;

    IF NEW.source_version_form = 'point_in_time_consolidation'
       AND parent_source_class NOT IN ('tax_law', 'regulation') THEN
        RAISE EXCEPTION 'point_in_time_consolidation is allowed only for tax_law and regulation';
    END IF;

    IF NEW.supersedes_source_version_id IS NOT NULL THEN
        SELECT source_id
        INTO superseded_source_id
        FROM knowledge_source_versions
        WHERE id = NEW.supersedes_source_version_id;

        IF superseded_source_id IS NULL THEN
            RAISE EXCEPTION 'knowledge_source_versions supersession requires existing predecessor';
        END IF;

        IF superseded_source_id <> NEW.source_id THEN
            RAISE EXCEPTION 'knowledge_source_versions supersession requires same source_id family';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_knowledge_source_versions_enforce_governed_rules
    ON knowledge_source_versions;

CREATE TRIGGER trg_knowledge_source_versions_enforce_governed_rules
    BEFORE INSERT OR UPDATE ON knowledge_source_versions
    FOR EACH ROW
    EXECUTE FUNCTION fn_knowledge_source_versions_enforce_governed_rules();

CREATE OR REPLACE FUNCTION fn_knowledge_source_versions_prevent_searchable_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.publication_state IN ('published', 'superseded') THEN
        IF OLD.source_id IS DISTINCT FROM NEW.source_id
           OR OLD.document_id IS DISTINCT FROM NEW.document_id
           OR OLD.point_in_time_url IS DISTINCT FROM NEW.point_in_time_url
           OR OLD.source_checksum_sha256 IS DISTINCT FROM NEW.source_checksum_sha256
           OR OLD.source_version_form IS DISTINCT FROM NEW.source_version_form
           OR OLD.source_input_origin IS DISTINCT FROM NEW.source_input_origin
           OR OLD.source_input_ref IS DISTINCT FROM NEW.source_input_ref
           OR OLD.effective_from IS DISTINCT FROM NEW.effective_from
           OR OLD.effective_to IS DISTINCT FROM NEW.effective_to
           OR OLD.tax_year IS DISTINCT FROM NEW.tax_year
           OR OLD.publication_event_id IS DISTINCT FROM NEW.publication_event_id
           OR OLD.supersedes_source_version_id IS DISTINCT FROM NEW.supersedes_source_version_id THEN
            RAISE EXCEPTION 'published knowledge_source_versions are immutable outside governed supersession state changes';
        END IF;

        IF OLD.publication_state = 'published'
           AND NEW.publication_state NOT IN ('published', 'superseded', 'archived') THEN
            RAISE EXCEPTION 'published knowledge_source_versions may only remain published or transition to superseded or archived';
        END IF;

        IF OLD.publication_state = 'superseded'
           AND NEW.publication_state NOT IN ('superseded', 'archived') THEN
            RAISE EXCEPTION 'superseded knowledge_source_versions may only remain superseded or transition to archived';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_knowledge_source_versions_prevent_searchable_mutation
    ON knowledge_source_versions;

CREATE TRIGGER trg_knowledge_source_versions_prevent_searchable_mutation
    BEFORE UPDATE ON knowledge_source_versions
    FOR EACH ROW
    EXECUTE FUNCTION fn_knowledge_source_versions_prevent_searchable_mutation();

CREATE OR REPLACE FUNCTION fn_knowledge_source_versions_prevent_searchable_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.publication_state IN ('published', 'superseded') THEN
        RAISE EXCEPTION 'published knowledge_source_versions cannot be deleted';
    END IF;
    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_knowledge_source_versions_prevent_searchable_delete
    ON knowledge_source_versions;

CREATE TRIGGER trg_knowledge_source_versions_prevent_searchable_delete
    BEFORE DELETE ON knowledge_source_versions
    FOR EACH ROW
    EXECUTE FUNCTION fn_knowledge_source_versions_prevent_searchable_delete();

CREATE OR REPLACE FUNCTION fn_knowledge_anchors_enforce_searchable_parent()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    parent_effective_from DATE;
    parent_effective_to DATE;
    parent_publication_state TEXT;
BEGIN
    SELECT
        effective_from,
        effective_to,
        publication_state
    INTO
        parent_effective_from,
        parent_effective_to,
        parent_publication_state
    FROM knowledge_source_versions
    WHERE id = NEW.source_version_id;

    IF parent_publication_state IS NULL THEN
        RAISE EXCEPTION 'knowledge_anchors requires existing source_version lineage';
    END IF;

    IF parent_publication_state NOT IN ('published', 'superseded') THEN
        RAISE EXCEPTION 'knowledge_anchors parent source_version must be in searchable state';
    END IF;

    IF NEW.temporal_scope_from < parent_effective_from THEN
        RAISE EXCEPTION 'knowledge_anchors temporal scope must remain within parent source_version window';
    END IF;

    IF parent_effective_to IS NOT NULL
       AND (
            NEW.temporal_scope_to IS NULL
            OR NEW.temporal_scope_to > parent_effective_to
       ) THEN
        RAISE EXCEPTION 'knowledge_anchors temporal scope must remain within parent source_version window';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_knowledge_anchors_enforce_searchable_parent
    ON knowledge_anchors;

CREATE TRIGGER trg_knowledge_anchors_enforce_searchable_parent
    BEFORE INSERT OR UPDATE ON knowledge_anchors
    FOR EACH ROW
    EXECUTE FUNCTION fn_knowledge_anchors_enforce_searchable_parent();

CREATE OR REPLACE FUNCTION fn_knowledge_anchors_prevent_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'knowledge_anchors are immutable after creation';
END;
$$;

DROP TRIGGER IF EXISTS trg_knowledge_anchors_prevent_update ON knowledge_anchors;
CREATE TRIGGER trg_knowledge_anchors_prevent_update
    BEFORE UPDATE ON knowledge_anchors
    FOR EACH ROW
    EXECUTE FUNCTION fn_knowledge_anchors_prevent_mutation();

DROP TRIGGER IF EXISTS trg_knowledge_anchors_prevent_delete ON knowledge_anchors;
CREATE TRIGGER trg_knowledge_anchors_prevent_delete
    BEFORE DELETE ON knowledge_anchors
    FOR EACH ROW
    EXECUTE FUNCTION fn_knowledge_anchors_prevent_mutation();

CREATE OR REPLACE FUNCTION fn_knowledge_chunks_enforce_searchable_parent()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    parent_publication_state TEXT;
BEGIN
    SELECT ksv.publication_state
    INTO parent_publication_state
    FROM knowledge_anchors AS ka
    JOIN knowledge_source_versions AS ksv
      ON ksv.id = ka.source_version_id
    WHERE ka.anchor_id = NEW.anchor_id;

    IF parent_publication_state IS NULL THEN
        RAISE EXCEPTION 'knowledge_chunks requires existing anchor lineage';
    END IF;

    IF parent_publication_state NOT IN ('published', 'superseded') THEN
        RAISE EXCEPTION 'knowledge_chunks parent anchor must inherit searchable source_version state';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_knowledge_chunks_enforce_searchable_parent
    ON knowledge_chunks;

CREATE TRIGGER trg_knowledge_chunks_enforce_searchable_parent
    BEFORE INSERT OR UPDATE ON knowledge_chunks
    FOR EACH ROW
    EXECUTE FUNCTION fn_knowledge_chunks_enforce_searchable_parent();

CREATE OR REPLACE FUNCTION fn_knowledge_chunks_prevent_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'knowledge_chunks are immutable after creation';
END;
$$;

DROP TRIGGER IF EXISTS trg_knowledge_chunks_prevent_update ON knowledge_chunks;
CREATE TRIGGER trg_knowledge_chunks_prevent_update
    BEFORE UPDATE ON knowledge_chunks
    FOR EACH ROW
    EXECUTE FUNCTION fn_knowledge_chunks_prevent_mutation();

DROP TRIGGER IF EXISTS trg_knowledge_chunks_prevent_delete ON knowledge_chunks;
CREATE TRIGGER trg_knowledge_chunks_prevent_delete
    BEFORE DELETE ON knowledge_chunks
    FOR EACH ROW
    EXECUTE FUNCTION fn_knowledge_chunks_prevent_mutation();

COMMIT;
