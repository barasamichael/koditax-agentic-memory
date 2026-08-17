"""Persistence models for the additive Milestone 1 document-domain foundation.

The legacy extraction repositories remain the production path until a later
caller-migration milestone.  This repository deliberately exposes only safe,
append-only creation operations for the target records.
"""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from threading import RLock
from dataclasses import dataclass

from pydantic import Field
from pydantic import BaseModel

from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction


class DocumentVersionCreate(BaseModel):
    """Define an immutable authoritative source-state version."""

    tenant_id: str = Field(min_length=1)
    document_id: UUID
    version_number: int = Field(gt=0)
    version_state: str = "current"
    supersedes_document_version_id: UUID | None = None


class SourceArtifactCreate(BaseModel):
    """Define an immutable authoritative original binary reference."""

    tenant_id: str = Field(min_length=1)
    document_version_id: UUID
    storage_key: str = Field(min_length=1)
    checksum_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    checksum_algorithm: str = "sha256"
    content_type: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    retention_state: str = "active"
    integrity_state: str = "verified"


@dataclass(frozen=True)
class DocumentVersionRecord:
    """One tenant-scoped immutable document version and lineage edge."""

    document_version_id: UUID
    document_id: UUID
    tenant_id: str
    version_number: int
    version_state: str
    supersedes_document_version_id: UUID | None
    idempotency_key: str | None


@dataclass(frozen=True)
class SourceArtifactRecord:
    """One tenant-scoped, immutable authoritative source artifact."""

    source_artifact_id: UUID
    document_version_id: UUID
    document_id: UUID
    tenant_id: str
    storage_key: str
    checksum_algorithm: str
    checksum_sha256: str
    verified_media_type: str
    size_bytes: int
    integrity_state: str
    retention_state: str


class SourceArtifactStoreProtocol:
    """The single source-artifact authority used by registration paths."""

    def register_source_artifact(
        self, *, document_id: UUID, record: SourceArtifactCreate, idempotency_key: str
    ) -> SourceArtifactRecord:
        """Atomically append an authoritative source artifact for a document."""

        raise NotImplementedError

    def get_source_artifact(
        self, *, tenant_id: str, source_artifact_id: UUID
    ) -> SourceArtifactRecord | None:
        """Resolve one source artifact through source-artifact identity."""

        raise NotImplementedError

    def get_source_artifact_for_version(
        self, *, tenant_id: str, document_version_id: UUID
    ) -> SourceArtifactRecord | None:
        """Resolve one source artifact through version-scoped identity."""

        raise NotImplementedError


class InMemorySourceArtifactStore(SourceArtifactStoreProtocol):
    """Reference source authority for explicit non-production test runtimes."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._by_command: dict[
            tuple[str, UUID, str], tuple[SourceArtifactCreate, SourceArtifactRecord]
        ] = {}
        self._by_source_artifact_id: dict[UUID, SourceArtifactRecord] = {}
        self._by_document_version_id: dict[UUID, SourceArtifactRecord] = {}

    def register_source_artifact(
        self, *, document_id: UUID, record: SourceArtifactCreate, idempotency_key: str
    ) -> SourceArtifactRecord:
        with self._lock:
            command_key = (record.tenant_id, document_id, idempotency_key)
            prior = self._by_command.get(command_key)
            if prior is not None:
                if prior[0] != record:
                    raise ValueError("source_artifact_idempotency_key_payload_mismatch")
                return prior[1]
            artifact = SourceArtifactRecord(
                source_artifact_id=uuid4(),
                document_version_id=record.document_version_id,
                document_id=document_id,
                tenant_id=record.tenant_id,
                storage_key=record.storage_key,
                checksum_algorithm=record.checksum_algorithm,
                checksum_sha256=record.checksum_sha256,
                verified_media_type=record.content_type,
                size_bytes=record.size_bytes,
                integrity_state=record.integrity_state,
                retention_state=record.retention_state,
            )
            self._by_command[command_key] = (record, artifact)
            self._by_source_artifact_id[artifact.source_artifact_id] = artifact
            self._by_document_version_id[artifact.document_version_id] = artifact
            return artifact

    def get_source_artifact(
        self, *, tenant_id: str, source_artifact_id: UUID
    ) -> SourceArtifactRecord | None:
        artifact = self._by_source_artifact_id.get(source_artifact_id)
        if artifact is None or artifact.tenant_id != tenant_id:
            return None
        return artifact

    def get_source_artifact_for_version(
        self, *, tenant_id: str, document_version_id: UUID
    ) -> SourceArtifactRecord | None:
        artifact = self._by_document_version_id.get(document_version_id)
        if artifact is None or artifact.tenant_id != tenant_id:
            return None
        return artifact


class PersistentDocumentFoundationStore:
    """Write target document versions and immutable source artifacts to PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def create_document_version(self, record: DocumentVersionCreate) -> UUID:
        """Persist one new version without changing the durable document identity."""

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.document_foundation.create_document_version",
            transaction_callback=lambda cursor: self._create_document_version_transaction(
                cursor=cursor, record=record
            ),
            reconcile_ambiguous_result=lambda connection: self._reconcile_document_version_result(
                connection=connection, record=record
            ),
        )

    def list_document_versions(
        self, *, tenant_id: str, document_id: UUID
    ) -> list[DocumentVersionRecord]:
        """Return reconstructable version history ordered by version number."""

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT document_version_id, document_id, tenant_id, version_number,
                           version_state, supersedes_document_version_id, idempotency_key
                    FROM document_ai_document_versions
                    WHERE tenant_id = %s AND document_id = %s
                    ORDER BY version_number ASC, created_at ASC
                    """,
                    (tenant_id, document_id),
                )
                rows = cursor.fetchall()
        return [
            DocumentVersionRecord(
                document_version_id=UUID(str(row[0])),
                document_id=UUID(str(row[1])),
                tenant_id=str(row[2]),
                version_number=int(row[3]),
                version_state=str(row[4]),
                supersedes_document_version_id=UUID(str(row[5])) if row[5] is not None else None,
                idempotency_key=str(row[6]) if row[6] is not None else None,
            )
            for row in rows
        ]

    def get_document_version(
        self, *, tenant_id: str, document_version_id: UUID
    ) -> DocumentVersionRecord | None:
        """Resolve one document version by tenant-scoped version identity."""

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT document_version_id, document_id, tenant_id, version_number,
                           version_state, supersedes_document_version_id, idempotency_key
                    FROM document_ai_document_versions
                    WHERE tenant_id = %s AND document_version_id = %s
                    """,
                    (tenant_id, document_version_id),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return DocumentVersionRecord(
            document_version_id=UUID(str(row[0])),
            document_id=UUID(str(row[1])),
            tenant_id=str(row[2]),
            version_number=int(row[3]),
            version_state=str(row[4]),
            supersedes_document_version_id=UUID(str(row[5])) if row[5] is not None else None,
            idempotency_key=str(row[6]) if row[6] is not None else None,
        )

    def _create_document_version_transaction(
        self, *, cursor: object, record: DocumentVersionCreate
    ) -> UUID:
        cursor.execute(
            """
            INSERT INTO document_ai_document_versions (
                tenant_id, document_id, version_number, version_state,
                supersedes_document_version_id
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING document_version_id
            """,
            (
                record.tenant_id,
                record.document_id,
                record.version_number,
                record.version_state,
                record.supersedes_document_version_id,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Document version insert did not return an identifier.")
        return UUID(str(row[0]))

    def _reconcile_document_version_result(
        self, *, connection: object, record: DocumentVersionCreate
    ) -> UUID | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT document_version_id
                FROM document_ai_document_versions
                WHERE tenant_id = %s AND document_id = %s AND version_number = %s
                """,
                (record.tenant_id, record.document_id, record.version_number),
            )
            row = cursor.fetchone()
        return None if row is None else UUID(str(row[0]))

    def create_source_artifact(self, record: SourceArtifactCreate) -> UUID:
        """Persist an immutable source-artifact reference for a document version."""

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.document_foundation.create_source_artifact",
            transaction_callback=lambda cursor: self._create_source_artifact_transaction(
                cursor=cursor, record=record
            ),
            reconcile_ambiguous_result=lambda connection: self._reconcile_source_artifact_result(
                connection=connection, record=record
            ),
        )

    def _create_source_artifact_transaction(
        self, *, cursor: object, record: SourceArtifactCreate
    ) -> UUID:
        cursor.execute(
            """
            INSERT INTO document_ai_source_artifacts (
                tenant_id, document_version_id, storage_key, checksum_sha256,
                checksum_algorithm, verified_media_type, content_type, size_bytes,
                retention_state, integrity_state
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING source_artifact_id
            """,
            (
                record.tenant_id,
                record.document_version_id,
                record.storage_key,
                record.checksum_sha256,
                record.checksum_algorithm,
                record.content_type,
                record.content_type,
                record.size_bytes,
                record.retention_state,
                record.integrity_state,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Source artifact insert did not return an identifier.")
        return UUID(str(row[0]))

    def _reconcile_source_artifact_result(
        self, *, connection: object, record: SourceArtifactCreate
    ) -> UUID | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT source_artifact_id
                FROM document_ai_source_artifacts
                WHERE tenant_id = %s AND document_version_id = %s
                  AND storage_key = %s AND checksum_sha256 = %s
                  AND checksum_algorithm = %s
                """,
                (
                    record.tenant_id,
                    record.document_version_id,
                    record.storage_key,
                    record.checksum_sha256,
                    record.checksum_algorithm,
                ),
            )
            row = cursor.fetchone()
        return None if row is None else UUID(str(row[0]))

    def register_source_artifact(
        self, *, document_id: UUID, record: SourceArtifactCreate, idempotency_key: str
    ) -> SourceArtifactRecord:
        """Append a version/artifact pair and select it as the document authority."""

        def reconcile_result(connection: object) -> SourceArtifactRecord | None:
            return self._reconcile_registered_source_artifact_result(
                connection=connection,
                document_id=document_id,
                record=record,
                idempotency_key=idempotency_key,
            )

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.document_foundation.register_source_artifact",
            transaction_callback=lambda cursor: self._register_source_artifact_transaction(
                cursor=cursor,
                document_id=document_id,
                record=record,
                idempotency_key=idempotency_key,
            ),
            reconcile_ambiguous_result=reconcile_result,
        )

    def _register_source_artifact_transaction(
        self,
        *,
        cursor: object,
        document_id: UUID,
        record: SourceArtifactCreate,
        idempotency_key: str,
    ) -> SourceArtifactRecord:
        cursor.execute(
            """
            SELECT document_version_id
            FROM document_ai_document_versions
            WHERE tenant_id = %s AND document_id = %s AND idempotency_key = %s
            """,
            (record.tenant_id, document_id, idempotency_key),
        )
        prior = cursor.fetchone()
        if prior is not None:
            cursor.execute(
                """
                SELECT source_artifact_id, storage_key, checksum_algorithm, checksum_sha256,
                       verified_media_type, size_bytes, integrity_state, retention_state
                FROM document_ai_source_artifacts
                WHERE tenant_id = %s AND document_version_id = %s
                """,
                (record.tenant_id, prior[0]),
            )
            artifact_row = cursor.fetchone()
            if artifact_row is None or (
                str(artifact_row[1]),
                str(artifact_row[2]),
                str(artifact_row[3]),
                str(artifact_row[4]),
                int(artifact_row[5]),
                str(artifact_row[6]),
                str(artifact_row[7]),
            ) != (
                record.storage_key,
                record.checksum_algorithm,
                record.checksum_sha256,
                record.content_type,
                record.size_bytes,
                record.integrity_state,
                record.retention_state,
            ):
                raise ValueError("source_artifact_idempotency_key_payload_mismatch")
            return SourceArtifactRecord(
                source_artifact_id=UUID(str(artifact_row[0])),
                document_version_id=UUID(str(prior[0])),
                document_id=document_id,
                tenant_id=record.tenant_id,
                storage_key=str(artifact_row[1]),
                checksum_algorithm=str(artifact_row[2]),
                checksum_sha256=str(artifact_row[3]),
                verified_media_type=str(artifact_row[4]),
                size_bytes=int(artifact_row[5]),
                integrity_state=str(artifact_row[6]),
                retention_state=str(artifact_row[7]),
            )
        cursor.execute(
            """
            SELECT active_document_version_id
            FROM document_ai_documents
            WHERE tenant_id = %s AND document_id = %s
            FOR UPDATE
            """,
            (record.tenant_id, document_id),
        )
        document_row = cursor.fetchone()
        if document_row is None:
            raise ValueError("document_not_found")
        current_version_id = (
            UUID(str(document_row[0])) if document_row[0] is not None else None
        )
        cursor.execute(
            """
            SELECT document_version_id, version_number
            FROM document_ai_document_versions
            WHERE tenant_id = %s AND document_id = %s
            ORDER BY version_number DESC, created_at DESC
            LIMIT 1
            FOR UPDATE
            """,
            (record.tenant_id, document_id),
        )
        latest_version_row = cursor.fetchone()
        supersedes_document_version_id = current_version_id
        if supersedes_document_version_id is None and latest_version_row is not None:
            supersedes_document_version_id = UUID(str(latest_version_row[0]))
        cursor.execute(
            """SELECT COALESCE(MAX(version_number), 0) + 1
               FROM document_ai_document_versions
               WHERE tenant_id = %s AND document_id = %s""",
            (record.tenant_id, document_id),
        )
        version_number_row = cursor.fetchone()
        if version_number_row is None:
            raise RuntimeError("document_version_number_missing")
        version_number = int(version_number_row[0])
        cursor.execute(
            """
            UPDATE document_ai_document_versions SET version_state = 'superseded'
            WHERE tenant_id = %s AND document_id = %s AND document_version_id = %s
            """,
            (record.tenant_id, document_id, supersedes_document_version_id),
        )
        cursor.execute(
            """
            INSERT INTO document_ai_document_versions (
                document_version_id, tenant_id, document_id, version_number, version_state,
                supersedes_document_version_id, idempotency_key
            ) VALUES (%s, %s, %s, %s, 'current', %s, %s)
            """,
            (
                record.document_version_id,
                record.tenant_id,
                document_id,
                version_number,
                supersedes_document_version_id,
                idempotency_key,
            ),
        )
        cursor.execute(
            """
            INSERT INTO document_ai_source_artifacts (
                tenant_id, document_version_id, storage_key, checksum_sha256,
                checksum_algorithm, verified_media_type, content_type, size_bytes,
                retention_state, integrity_state
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING source_artifact_id
            """,
            (
                record.tenant_id,
                record.document_version_id,
                record.storage_key,
                record.checksum_sha256,
                record.checksum_algorithm,
                record.content_type,
                record.content_type,
                record.size_bytes,
                record.retention_state,
                record.integrity_state,
            ),
        )
        artifact_id_row = cursor.fetchone()
        if artifact_id_row is None:
            raise RuntimeError("source_artifact_insert_missing_identifier")
        artifact_id = UUID(str(artifact_id_row[0]))
        cursor.execute(
            """
            UPDATE document_ai_documents
            SET active_document_version_id = %s
            WHERE tenant_id = %s AND document_id = %s
            """,
            (record.document_version_id, record.tenant_id, document_id),
        )
        return SourceArtifactRecord(
            source_artifact_id=artifact_id,
            document_version_id=record.document_version_id,
            document_id=document_id,
            tenant_id=record.tenant_id,
            storage_key=record.storage_key,
            checksum_algorithm=record.checksum_algorithm,
            checksum_sha256=record.checksum_sha256,
            verified_media_type=record.content_type,
            size_bytes=record.size_bytes,
            integrity_state=record.integrity_state,
            retention_state=record.retention_state,
        )

    def _reconcile_registered_source_artifact_result(
        self,
        *,
        connection: object,
        document_id: UUID,
        record: SourceArtifactCreate,
        idempotency_key: str,
    ) -> SourceArtifactRecord | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT version.document_version_id, artifact.source_artifact_id,
                       artifact.checksum_algorithm, artifact.checksum_sha256,
                       artifact.verified_media_type, artifact.size_bytes,
                       artifact.integrity_state, artifact.retention_state
                FROM document_ai_document_versions AS version
                JOIN document_ai_source_artifacts AS artifact
                  ON artifact.tenant_id = version.tenant_id
                 AND artifact.document_version_id = version.document_version_id
                WHERE version.tenant_id = %s AND version.document_id = %s
                  AND version.idempotency_key = %s
                """,
                (record.tenant_id, document_id, idempotency_key),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        if (
            str(row[2]),
            str(row[3]),
            str(row[4]),
            int(row[5]),
            str(row[6]),
            str(row[7]),
        ) != (
            record.checksum_algorithm,
            record.checksum_sha256,
            record.content_type,
            record.size_bytes,
            record.integrity_state,
            record.retention_state,
        ):
            return None
        return SourceArtifactRecord(
            source_artifact_id=UUID(str(row[1])),
            document_version_id=UUID(str(row[0])),
            document_id=document_id,
            tenant_id=record.tenant_id,
            storage_key=record.storage_key,
            checksum_algorithm=str(row[2]),
            checksum_sha256=str(row[3]),
            verified_media_type=str(row[4]),
            size_bytes=int(row[5]),
            integrity_state=str(row[6]),
            retention_state=str(row[7]),
        )

    def get_source_artifact(
        self, *, tenant_id: str, source_artifact_id: UUID
    ) -> SourceArtifactRecord | None:
        """Resolve authoritative source integrity without revealing its locator."""

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT artifact.source_artifact_id, artifact.document_version_id,
                           version.document_id, artifact.storage_key,
                           artifact.checksum_algorithm, artifact.checksum_sha256,
                           artifact.verified_media_type, artifact.size_bytes,
                           artifact.integrity_state, artifact.retention_state
                    FROM document_ai_source_artifacts AS artifact
                    JOIN document_ai_document_versions AS version
                      ON version.tenant_id = artifact.tenant_id
                     AND version.document_version_id = artifact.document_version_id
                    WHERE artifact.tenant_id = %s
                      AND artifact.source_artifact_id = %s
                    """,
                    (tenant_id, source_artifact_id),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return SourceArtifactRecord(
            source_artifact_id=UUID(str(row[0])),
            document_version_id=UUID(str(row[1])),
            document_id=UUID(str(row[2])),
            tenant_id=tenant_id,
            storage_key=str(row[3]),
            checksum_algorithm=str(row[4]),
            checksum_sha256=str(row[5]),
            verified_media_type=str(row[6]),
            size_bytes=int(row[7]),
            integrity_state=str(row[8]),
            retention_state=str(row[9]),
        )

    def get_source_artifact_for_version(
        self, *, tenant_id: str, document_version_id: UUID
    ) -> SourceArtifactRecord | None:
        """Resolve authoritative source integrity through version identity."""

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT artifact.source_artifact_id, artifact.document_version_id,
                           version.document_id, artifact.storage_key,
                           artifact.checksum_algorithm, artifact.checksum_sha256,
                           artifact.verified_media_type, artifact.size_bytes,
                           artifact.integrity_state, artifact.retention_state
                    FROM document_ai_source_artifacts AS artifact
                    JOIN document_ai_document_versions AS version
                      ON version.tenant_id = artifact.tenant_id
                     AND version.document_version_id = artifact.document_version_id
                    WHERE artifact.tenant_id = %s
                      AND artifact.document_version_id = %s
                    """,
                    (tenant_id, document_version_id),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return SourceArtifactRecord(
            source_artifact_id=UUID(str(row[0])),
            document_version_id=UUID(str(row[1])),
            document_id=UUID(str(row[2])),
            tenant_id=tenant_id,
            storage_key=str(row[3]),
            checksum_algorithm=str(row[4]),
            checksum_sha256=str(row[5]),
            verified_media_type=str(row[6]),
            size_bytes=int(row[7]),
            integrity_state=str(row[8]),
            retention_state=str(row[9]),
        )
