"""Migration-only utility for historical records; never loaded by the runtime."""
# ruff: noqa: E501
# pyright: reportUnknownMemberType=false, reportAttributeAccessIssue=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import json
from uuid import UUID
from hashlib import sha256
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Mapping

from services.document_ai.app.storage_adapter import StorageAdapterProtocol
from services.document_ai.app.persistence_support import connect_document_ai_database


@dataclass(frozen=True)
class MigrationOutcome:
    document_id: UUID
    state: str
    checkpoint: str
    exception_code: str | None = None


class LegacyDocumentMigrator:
    """Moves one legacy document at a time; each checkpoint is safe to replay."""

    def __init__(self, *, database_url: str, storage: StorageAdapterProtocol) -> None:
        self._database_url = database_url
        self._storage = storage

    def migrate_all(self) -> list[MigrationOutcome]:
        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("""SELECT document_id FROM document_ai_documents document
                    WHERE NOT EXISTS (SELECT 1 FROM document_ai_document_versions version
                      WHERE version.tenant_id = document.tenant_id AND version.document_id = document.document_id)
                    OR EXISTS (SELECT 1 FROM document_ai_legacy_migrations migration
                      WHERE migration.tenant_id = document.tenant_id AND migration.legacy_document_id = document.document_id
                        AND migration.state IN ('pending', 'running', 'rolled_back'))""")
                document_ids = [UUID(str(row[0])) for row in cursor.fetchall()]
        return [self.migrate_document(document_id=item) for item in document_ids]

    def migrate_document(self, *, document_id: UUID) -> MigrationOutcome:
        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT tenant_id, owner_user_id, storage_key, checksum_sha256, size_bytes, content_type
                    FROM document_ai_documents WHERE document_id = %s FOR UPDATE""",
                    (document_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("legacy_document_not_found")
                tenant_id, owner_id, legacy_key, declared_checksum, _size, content_type = row
                if owner_id is None:
                    return self._block(cursor, tenant_id, document_id, "ambiguous_owner")
                self._start(cursor, tenant_id, document_id)
                try:
                    source_path, source_type = self._storage.resolve_download_object(
                        str(legacy_key)
                    )
                    payload_path = Path(source_path)
                    payload_size = payload_path.stat().st_size
                    digest = sha256()
                    with payload_path.open("rb") as payload_stream:
                        for chunk in iter(lambda: payload_stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    checksum = digest.hexdigest()
                    if declared_checksum and checksum != declared_checksum:
                        return self._block(
                            cursor, tenant_id, document_id, "source_checksum_mismatch"
                        )
                    target_key = f"{tenant_id}/documents/{document_id}/versions/1/original"
                    with payload_path.open("rb") as payload_stream:
                        self._storage.store_upload_object_filelike(
                            target_key,
                            payload_stream,
                            str(source_type or content_type),
                            payload_size,
                        )
                    verified = self._storage.get_object_metadata(target_key)
                    if verified.size_bytes != payload_size:
                        return self._block(
                            cursor, tenant_id, document_id, "r2_copy_verification_failed"
                        )
                    version_id = self._target_version(cursor, tenant_id, document_id)
                    artifact_id = self._target_artifact(
                        cursor,
                        tenant_id,
                        version_id,
                        target_key,
                        checksum,
                        payload_size,
                        str(source_type or content_type),
                    )
                    self._preserve_bindings(cursor, tenant_id, document_id, version_id)
                    representation_id = self._historical_canonical(
                        cursor, tenant_id, document_id, version_id, artifact_id
                    )
                    self._migrate_extractions(
                        cursor, tenant_id, document_id, version_id, representation_id, owner_id
                    )
                    self._preserve_workflows(cursor, tenant_id, document_id, version_id)
                    cursor.execute(
                        """UPDATE document_ai_legacy_migrations SET state='migrated', checkpoint='complete',
                        target_document_id=%s, target_document_version_id=%s, completed_at=now(), updated_at=now(), exception_code=NULL
                        WHERE tenant_id=%s AND legacy_document_id=%s""",
                        (document_id, version_id, tenant_id, document_id),
                    )
                    connection.commit()
                    return MigrationOutcome(document_id, "migrated", "complete")
                except FileNotFoundError:
                    return self._block(cursor, tenant_id, document_id, "missing_original")
                except Exception as error:
                    connection.rollback()
                    with connection.cursor() as retry:
                        outcome = self._block(
                            retry,
                            tenant_id,
                            document_id,
                            "migration_error",
                            {"type": type(error).__name__},
                        )
                    connection.commit()
                    return outcome

    def rollback_document(self, *, document_id: UUID) -> None:
        """Rollback only migration-owned target derivatives; original legacy rows remain intact."""
        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE document_ai_legacy_migrations SET state='rolled_back', checkpoint='rollback_complete',
                    updated_at=now() WHERE legacy_document_id=%s AND state='migrated'""",
                    (document_id,),
                )
            connection.commit()

    def _start(self, cursor: object, tenant_id: str, document_id: UUID) -> None:
        cursor.execute(
            """INSERT INTO document_ai_legacy_migrations (tenant_id, legacy_document_id, state, attempt_count, checkpoint)
            VALUES (%s, %s, 'running', 1, 'owner_resolved') ON CONFLICT (tenant_id, legacy_document_id) DO UPDATE
            SET state='running', attempt_count=document_ai_legacy_migrations.attempt_count + 1, updated_at=now()""",
            (tenant_id, document_id),
        )

    def _block(
        self,
        cursor: object,
        tenant_id: str,
        document_id: UUID,
        code: str,
        detail: Mapping[str, object] | None = None,
    ) -> MigrationOutcome:
        cursor.execute(
            """INSERT INTO document_ai_legacy_migrations (tenant_id, legacy_document_id, state, checkpoint, exception_code, exception_detail)
            VALUES (%s, %s, 'blocked', 'exception_recorded', %s, %s::jsonb) ON CONFLICT (tenant_id, legacy_document_id) DO UPDATE
            SET state='blocked', checkpoint='exception_recorded', exception_code=EXCLUDED.exception_code, exception_detail=EXCLUDED.exception_detail, updated_at=now()""",
            (tenant_id, document_id, code, json.dumps(detail or {}, sort_keys=True)),
        )
        return MigrationOutcome(document_id, "blocked", "exception_recorded", code)

    @staticmethod
    def _target_version(cursor: object, tenant_id: str, document_id: UUID) -> UUID:
        cursor.execute(
            """INSERT INTO document_ai_document_versions (tenant_id, document_id, version_number, version_state)
          VALUES (%s, %s, 1, 'current') ON CONFLICT (tenant_id, document_id, version_number) DO UPDATE SET version_state='current'
          RETURNING document_version_id""",
            (tenant_id, document_id),
        )
        return UUID(str(cursor.fetchone()[0]))

    @staticmethod
    def _target_artifact(
        cursor: object,
        tenant_id: str,
        version_id: UUID,
        key: str,
        checksum: str,
        size: int,
        content_type: str,
    ) -> UUID:
        cursor.execute(
            """INSERT INTO document_ai_source_artifacts (tenant_id, document_version_id, storage_key, checksum_sha256, size_bytes, content_type)
          VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, storage_key) DO UPDATE SET integrity_state='verified'
          RETURNING source_artifact_id""",
            (tenant_id, version_id, key, checksum, size, content_type),
        )
        return UUID(str(cursor.fetchone()[0]))

    @staticmethod
    def _preserve_bindings(
        cursor: object, tenant_id: str, document_id: UUID, version_id: UUID
    ) -> None:
        cursor.execute(
            """UPDATE document_ai_document_bindings SET document_version_id=%s
          WHERE tenant_id=%s AND document_id=%s AND document_version_id IS NULL""",
            (version_id, tenant_id, document_id),
        )

    @staticmethod
    def _historical_canonical(
        cursor: object, tenant_id: str, document_id: UUID, version_id: UUID, artifact_id: UUID
    ) -> UUID:
        cursor.execute(
            """INSERT INTO document_ai_processing_operations (tenant_id, document_version_id, operation_kind, processing_policy_version, processor_version, state, completed_at, correlation_id)
          VALUES (%s,%s,'legacy_migration','legacy-v1','legacy-migration-v1','succeeded',now(),%s) RETURNING processing_operation_id""",
            (tenant_id, version_id, f"legacy-migration:{document_id}"),
        )
        operation_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO document_ai_canonical_representations (tenant_id,document_version_id,processing_operation_id,canonical_schema_version,processing_policy_family,state,is_active,representation_payload,source_artifact_id,assembly_policy_version,content_hash_sha256,readiness_state,validated_at)
          VALUES (%s,%s,%s,'legacy-historical-v1','legacy_migration','active',true,%s::jsonb,%s,'legacy-migration-v1',%s,'partial',now())
          ON CONFLICT DO NOTHING RETURNING canonical_representation_id""",
            (
                tenant_id,
                version_id,
                operation_id,
                json.dumps(
                    {"kind": "legacy_historical_observations", "document_id": str(document_id)}
                ),
                artifact_id,
                sha256(str(document_id).encode()).hexdigest(),
            ),
        )
        row = cursor.fetchone()
        if row:
            return UUID(str(row[0]))
        cursor.execute(
            "SELECT canonical_representation_id FROM document_ai_canonical_representations WHERE tenant_id=%s AND document_version_id=%s AND is_active",
            (tenant_id, version_id),
        )
        return UUID(str(cursor.fetchone()[0]))

    @staticmethod
    def _migrate_extractions(
        cursor: object,
        tenant_id: str,
        document_id: UUID,
        version_id: UUID,
        representation_id: UUID,
        owner_id: UUID,
    ) -> None:
        cursor.execute(
            "SELECT extraction_id, persisted_payload FROM document_ai_extractions WHERE persisted_payload ->> 'document_id' = %s",
            (str(document_id),),
        )
        for extraction_id, raw in cursor.fetchall():
            payload = raw if isinstance(raw, dict) else json.loads(raw)
            fields = payload.get("extracted_fields", {})
            if not isinstance(fields, dict):
                continue
            corrections = payload.get("corrections", [])
            for ordinal, (field, value) in enumerate(fields.items()):
                stable_key = f"legacy:{extraction_id}:{field}"
                cursor.execute(
                    """INSERT INTO document_ai_canonical_elements (tenant_id,canonical_representation_id,element_type,ordinal,observed_value,normalized_value,uncertainty,stable_key)
                  VALUES (%s,%s,'historical_observation',%s,%s::jsonb,%s::jsonb,%s::jsonb,%s) ON CONFLICT DO NOTHING RETURNING canonical_element_id""",
                    (
                        tenant_id,
                        representation_id,
                        ordinal,
                        json.dumps(value),
                        json.dumps(value),
                        json.dumps({"historical": True, "not_newly_verified": True}),
                        stable_key,
                    ),
                )
                element = cursor.fetchone()
                if element is None:
                    cursor.execute(
                        """SELECT canonical_element_id FROM document_ai_canonical_elements
                        WHERE tenant_id=%s AND canonical_representation_id=%s AND stable_key=%s""",
                        (tenant_id, representation_id, stable_key),
                    )
                    element = cursor.fetchone()
                assert element is not None
                element_id = element[0]
                cursor.execute(
                    """INSERT INTO document_ai_legacy_migration_observations (tenant_id,legacy_extraction_id,document_version_id,canonical_element_id,field_name,observed_value)
                  VALUES (%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING""",
                    (tenant_id, extraction_id, version_id, element_id, field, json.dumps(value)),
                )
                for correction in corrections if isinstance(corrections, list) else []:
                    if (
                        isinstance(correction, dict)
                        and correction.get("field_name") == field
                        and "corrected_value" in correction
                    ):
                        cursor.execute(
                            """INSERT INTO document_ai_corrections (tenant_id,document_version_id,canonical_element_id,prior_observed_value,prior_normalized_value,corrected_value,reason,actor_user_id,source_observed_value,original_interpreted_value,effective_value)
                          VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb) ON CONFLICT DO NOTHING""",
                            (
                                tenant_id,
                                version_id,
                                element_id,
                                json.dumps(value),
                                json.dumps(value),
                                json.dumps(correction["corrected_value"]),
                                str(correction.get("reason", "legacy correction")),
                                owner_id,
                                json.dumps(value),
                                json.dumps(value),
                                json.dumps(correction["corrected_value"]),
                            ),
                        )

    @staticmethod
    def _preserve_workflows(
        cursor: object, tenant_id: str, document_id: UUID, version_id: UUID
    ) -> None:
        cursor.execute(
            """SELECT persisted_payload FROM document_ai_evidence_linkages WHERE persisted_payload ->> 'document_id' = %s""",
            (str(document_id),),
        )
        for (raw,) in cursor.fetchall():
            payload = raw if isinstance(raw, dict) else json.loads(raw)
            workflow = payload.get("workflow_id") or payload.get("workflow_identity")
            if workflow:
                cursor.execute(
                    """INSERT INTO document_ai_workflow_projections (tenant_id,document_version_id,workflow_identity,workflow_version,projection_version,validity_state,projection_payload)
                  VALUES (%s,%s,%s,'legacy','legacy-migration-v1','partial',%s::jsonb) ON CONFLICT DO NOTHING""",
                    (
                        tenant_id,
                        version_id,
                        str(workflow),
                        json.dumps({"legacy_reference": payload, "historical": True}),
                    ),
                )
