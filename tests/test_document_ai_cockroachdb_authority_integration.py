from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Any
from typing import cast
from hashlib import sha256
from pathlib import Path
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
import pytest
import psycopg

from services.document_ai.app.config import get_document_ai_s3_bucket
from services.document_ai.app.config import get_document_ai_runtime_mode
from services.document_ai.app.config import get_document_ai_storage_provider
from services.document_ai.app.storage_keys import build_tenant_document_object_key
from services.document_ai.app.signed_access import PersistentSignedAccessStore
from services.document_ai.app.storage_adapter import S3StorageAdapter
from services.document_ai.app.storage_adapter import StorageUploadCapability
from services.document_ai.app.storage_adapter import build_runtime_storage_adapter
from services.document_ai.app.upload_sessions import UploadSessionRecord
from services.document_ai.app.upload_sessions import build_upload_session
from services.document_ai.app.upload_sessions import UploadSessionResponse
from services.document_ai.app.upload_sessions import UploadSessionConflictError
from services.document_ai.app.upload_sessions import UploadSessionCreateRequest
from services.document_ai.app.upload_sessions import PersistentUploadSessionStore
from services.document_ai.app.document_bindings import DocumentBindingRequest
from services.document_ai.app.document_bindings import DocumentBindingConflictError
from services.document_ai.app.document_bindings import PersistentDocumentBindingStore
from services.document_ai.app.document_registry import PersistedDocumentRecord
from services.document_ai.app.document_registry import UploadCompletionRequest
from services.document_ai.app.document_registry import CompletionValidationError
from services.document_ai.app.document_registry import register_upload_completion
from services.document_ai.app.document_registry import PersistentDocumentRegistryStore
from services.document_ai.app.document_registry import register_durable_upload_confirmation
from services.document_ai.app.document_lifecycle import DocumentLifecycleState
from services.document_ai.migrations.cockroachdb import runner
from services.document_ai.app.document_foundation import DocumentVersionCreate
from services.document_ai.app.document_foundation import SourceArtifactCreate
from services.document_ai.app.document_foundation import PersistentDocumentFoundationStore
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import load_document_ai_database_url
from services.document_ai.app.persistence_support import close_document_ai_connection_pool

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@pytest.fixture(scope="session")
def cockroach_document_ai_database() -> str:
    database_url = load_document_ai_database_url()
    if not database_url:
        pytest.skip("DATABASE_URL is not configured for Document AI CockroachDB tests.")

    with psycopg.connect(database_url) as connection:
        try:
            runner._validate_target_database(connection)  # type: ignore[arg-type]
        except runner.DocumentAITargetError:
            pytest.skip("DATABASE_URL does not target the expected CockroachDB kodi_dev database.")

    assert runner.main() == 0
    assert runner.main() == 0
    close_document_ai_connection_pool(database_url=database_url)
    return database_url


class _StaticStorageAdapter:
    def create_upload_capability(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        document_id: UUID,
        session_id: UUID,
        expires_at: str,
    ) -> StorageUploadCapability:
        del owner_user_id
        object_key = build_tenant_document_object_key(tenant_id, document_id)
        return StorageUploadCapability(
            capability_id=str(session_id),
            object_key=object_key,
            upload_url=f"https://storage.local/upload/{object_key}",
            expires_at=expires_at,
            storage_provider="in_memory",
            headers={"x-kodi-capability-id": str(session_id)},
        )


def _load_document_ai_document(
    *, database_url: str, document_id: UUID
) -> PersistedDocumentRecord | None:
    with connect_document_ai_database(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    document_id,
                    tenant_id,
                    owner_user_id,
                    state,
                    storage_key,
                    uploaded_at,
                    checksum_sha256,
                    size_bytes,
                    content_type,
                    computation_id,
                    purge_eligible_at,
                    purged_at,
                    compliance_lock_until,
                    display_name,
                    category,
                    tags,
                    description,
                    revision
                FROM document_ai_documents
                WHERE document_id = %s
                """,
                (document_id,),
            )
            row = cursor.fetchone()
    if row is None:
        return None
    return PersistedDocumentRecord(
        document_id=UUID(str(row[0])),
        tenant_id=str(row[1]),
        owner_user_id=UUID(str(row[2])),
        state=cast(DocumentLifecycleState, str(row[3])),
        storage_key=str(row[4]),
        uploaded_at=str(row[5]),
        checksum_sha256=str(row[6]),
        size_bytes=int(row[7]),
        content_type=str(row[8]),
        computation_id=None if row[9] is None else str(row[9]),
        purge_eligible_at=None if row[10] is None else str(row[10]),
        purged_at=None if row[11] is None else str(row[11]),
        compliance_lock_until=None if row[12] is None else str(row[12]),
        display_name=None if row[13] is None else str(row[13]),
        category=None if row[14] is None else str(row[14]),
        tags=list(row[15]) if row[15] is not None else [],
        description=None if row[16] is None else str(row[16]),
        revision=int(row[17]),
    )


def _store_document_ai_document(
    *, database_url: str, document_record: PersistedDocumentRecord
) -> None:
    with connect_document_ai_database(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO document_ai_documents (
                    document_id,
                    tenant_id,
                    owner_user_id,
                    state,
                    storage_key,
                    uploaded_at,
                    checksum_sha256,
                    size_bytes,
                    content_type,
                    computation_id,
                    purge_eligible_at,
                    purged_at,
                    compliance_lock_until,
                    display_name,
                    category,
                    tags,
                    description,
                    revision
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (document_id) DO UPDATE SET
                    tenant_id = EXCLUDED.tenant_id,
                    owner_user_id = EXCLUDED.owner_user_id,
                    state = EXCLUDED.state,
                    storage_key = EXCLUDED.storage_key,
                    uploaded_at = EXCLUDED.uploaded_at,
                    checksum_sha256 = EXCLUDED.checksum_sha256,
                    size_bytes = EXCLUDED.size_bytes,
                    content_type = EXCLUDED.content_type,
                    computation_id = EXCLUDED.computation_id,
                    purge_eligible_at = EXCLUDED.purge_eligible_at,
                    purged_at = EXCLUDED.purged_at,
                    compliance_lock_until = EXCLUDED.compliance_lock_until,
                    display_name = EXCLUDED.display_name,
                    category = EXCLUDED.category,
                    tags = EXCLUDED.tags,
                    description = EXCLUDED.description,
                    revision = EXCLUDED.revision
                """,
                (
                    document_record.document_id,
                    document_record.tenant_id,
                    document_record.owner_user_id,
                    document_record.state,
                    document_record.storage_key,
                    _parse_iso_datetime(document_record.uploaded_at),
                    document_record.checksum_sha256,
                    document_record.size_bytes,
                    document_record.content_type,
                    document_record.computation_id,
                    _parse_iso_datetime_or_none(document_record.purge_eligible_at),
                    _parse_iso_datetime_or_none(document_record.purged_at),
                    _parse_iso_datetime_or_none(document_record.compliance_lock_until),
                    document_record.display_name,
                    document_record.category,
                    document_record.tags,
                    document_record.description,
                    document_record.revision,
                ),
            )
        connection.commit()


def test_upload_session_idempotency_round_trips_through_cockroachdb(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    store = PersistentUploadSessionStore(database_url=database_url)
    request = UploadSessionCreateRequest(
        tenant_id=f"tenant-{uuid4().hex[:8]}",
        owner_user_id=uuid4(),
        file_name="invoice.pdf",
        content_type="application/pdf",
        expected_size_bytes=1024,
        checksum_sha256="a" * 64,
    )
    idempotency_key = f"idem-{uuid4().hex}"
    session_record: UploadSessionRecord | None = None

    try:
        response = build_upload_session(
            upload_session_request=request,
            principal_user_id=request.owner_user_id,
            idempotency_key=idempotency_key,
            correlation_id="corr-upload-session",
            upload_session_store=store,
            storage_adapter=cast(Any, _StaticStorageAdapter()),
        )
        replay = build_upload_session(
            upload_session_request=request,
            principal_user_id=request.owner_user_id,
            idempotency_key=idempotency_key,
            correlation_id="corr-upload-session-replay",
            upload_session_store=store,
            storage_adapter=cast(Any, _StaticStorageAdapter()),
        )
        conflict_request = request.model_copy(update={"file_name": "invoice-v2.pdf"})

        assert replay.model_dump(mode="json") == response.model_dump(mode="json")
        session_record = store.get_session(response.session_id)
        assert session_record is not None
        assert session_record.original_filename == request.file_name
        assert session_record.storage_key == build_tenant_document_object_key(
            request.tenant_id, response.document_id
        )
        assert session_record.storage_provider == "in_memory"
        assert session_record.upload_headers["x-kodi-capability-id"] == str(response.session_id)
        with pytest.raises(UploadSessionConflictError):
            build_upload_session(
                upload_session_request=conflict_request,
                principal_user_id=request.owner_user_id,
                idempotency_key=idempotency_key,
                correlation_id="corr-upload-session-conflict",
                upload_session_store=store,
                storage_adapter=cast(Any, _StaticStorageAdapter()),
            )

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT idempotency_key, session_id, document_id, session_record
                    FROM document_ai_upload_sessions
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                row = cursor.fetchone()
        assert row is not None
        assert str(row[0]) == idempotency_key
        assert UUID(str(row[1])) == response.session_id
        assert UUID(str(row[2])) == response.document_id
        assert row[3]["storage_key"] == build_tenant_document_object_key(
            request.tenant_id, response.document_id
        )
    finally:
        _cleanup_document_ai_authority_state(
            database_url=database_url,
            session_id=session_record.session_id if session_record is not None else None,
        )


def test_upload_session_idempotency_is_safe_under_concurrent_replay(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    store = PersistentUploadSessionStore(database_url=database_url)
    request = UploadSessionCreateRequest(
        tenant_id=f"tenant-{uuid4().hex[:8]}",
        owner_user_id=uuid4(),
        file_name="invoice.pdf",
        content_type="application/pdf",
        expected_size_bytes=1024,
        checksum_sha256="b" * 64,
    )
    idempotency_key = f"idem-{uuid4().hex}"
    barrier = Barrier(2)

    def _create_session() -> UploadSessionResponse:
        barrier.wait(timeout=15)
        return build_upload_session(
            upload_session_request=request,
            principal_user_id=request.owner_user_id,
            idempotency_key=idempotency_key,
            correlation_id="corr-upload-session-concurrent",
            upload_session_store=store,
            storage_adapter=cast(Any, _StaticStorageAdapter()),
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: _create_session(), range(2)))

        assert responses[0].model_dump(mode="json") == responses[1].model_dump(mode="json")

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_ai_upload_sessions
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                row = cursor.fetchone()

        assert row == (1,)
    finally:
        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM document_ai_upload_sessions
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
            connection.commit()


def test_upload_completion_persists_document_version_source_lineage_and_replays(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    registry_store = PersistentDocumentRegistryStore(database_url=database_url)
    foundation_store = PersistentDocumentFoundationStore(database_url=database_url)
    tenant_id = f"tenant-{uuid4().hex[:8]}"
    owner_user_id = uuid4()
    session_id = uuid4()
    document_id = uuid4()
    idempotency_key = f"idem-{uuid4().hex}"
    checksum_sha256 = "b" * 64
    object_key = build_tenant_document_object_key(tenant_id, document_id)
    upload_completion_request = UploadCompletionRequest(
        session_id=session_id,
        object_key=object_key,
        checksum_sha256=checksum_sha256,
        size_bytes=2048,
        content_type="application/pdf",
    )
    session_record = UploadSessionRecord(
        session_id=session_id,
        document_id=document_id,
        session_state="active",
        created_at=_utc_now_iso(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        content_type="application/pdf",
        expected_size_bytes=2048,
        checksum_sha256=checksum_sha256,
        expires_at=_utc_future_iso(minutes=15),
        original_filename="invoice.pdf",
        storage_provider="in_memory",
        storage_key=object_key,
        completed_at=None,
    )
    expected_version_id = uuid5(
        NAMESPACE_URL,
        f"document-ai:{tenant_id}:{document_id}:{idempotency_key}",
    )

    registry_store.get_document = lambda document_id_value: _load_document_ai_document(
        database_url=database_url,
        document_id=document_id_value,
    )  # type: ignore[method-assign]
    registry_store.set_document = lambda document_record: _store_document_ai_document(
        database_url=database_url,
        document_record=document_record,
    )  # type: ignore[method-assign]

    try:
        response = register_upload_completion(
            upload_completion_request=upload_completion_request,
            session_record=session_record,
            principal_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            correlation_id="corr-upload-completion",
            document_registry_store=registry_store,
            source_artifact_store=cast(Any, foundation_store),
        )
        replay = register_upload_completion(
            upload_completion_request=upload_completion_request,
            session_record=session_record,
            principal_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            correlation_id="corr-upload-completion-replay",
            document_registry_store=registry_store,
            source_artifact_store=cast(Any, foundation_store),
        )
        conflict_request = upload_completion_request.model_copy(
            update={"object_key": f"{tenant_id}/uploads/{uuid4().hex}.pdf"}
        )

        assert replay.model_dump(mode="json") == response.model_dump(mode="json")
        assert response.document.document_id == document_id
        assert response.document.state == "processing"
        assert response.processing_operation_id is None

        with pytest.raises(CompletionValidationError, match="object_key_mismatch"):
            register_upload_completion(
                upload_completion_request=conflict_request,
                session_record=session_record,
                principal_user_id=owner_user_id,
                idempotency_key=idempotency_key,
                correlation_id="corr-upload-completion-conflict",
                document_registry_store=registry_store,
                source_artifact_store=cast(Any, foundation_store),
            )

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT document_id, tenant_id, owner_user_id, state, active_document_version_id
                    FROM document_ai_documents
                    WHERE document_id = %s
                    """,
                    (document_id,),
                )
                document_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT document_version_id, tenant_id, document_id, version_number,
                           idempotency_key
                    FROM document_ai_document_versions
                    WHERE tenant_id = %s AND document_id = %s
                    """,
                    (tenant_id, document_id),
                )
                version_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT source_artifact_id, tenant_id, document_version_id, storage_key,
                           checksum_sha256, checksum_algorithm, verified_media_type
                    FROM document_ai_source_artifacts
                    WHERE tenant_id = %s AND document_version_id = %s
                    """,
                    (tenant_id, expected_version_id),
                )
                artifact_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT idempotency_key, request_fingerprint, response_payload
                    FROM document_ai_completion_idempotency
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                completion_row = cursor.fetchone()

        assert document_row is not None
        assert UUID(str(document_row[0])) == document_id
        assert str(document_row[3]) == "processing"
        assert UUID(str(document_row[4])) == expected_version_id

        assert version_row is not None
        assert UUID(str(version_row[0])) == expected_version_id
        assert int(version_row[3]) == 1
        assert str(version_row[4]) == idempotency_key

        assert artifact_row is not None
        assert UUID(str(artifact_row[2])) == expected_version_id
        assert str(artifact_row[3]) == object_key
        assert str(artifact_row[4]) == checksum_sha256
        assert str(artifact_row[5]) == "sha256"
        assert str(artifact_row[6]) == upload_completion_request.content_type

        assert completion_row is not None
        assert str(completion_row[0]) == idempotency_key
    finally:
        _cleanup_document_ai_authority_state(
            database_url=database_url,
            document_id=document_id,
            completion_idempotency_key=idempotency_key,
        )


def test_later_source_version_preserves_prior_version_lineage_and_source_identity(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    registry_store = PersistentDocumentRegistryStore(database_url=database_url)
    foundation_store = PersistentDocumentFoundationStore(database_url=database_url)
    tenant_id = f"tenant-{uuid4().hex[:8]}"
    owner_user_id = uuid4()
    session_id = uuid4()
    document_id = uuid4()
    first_idempotency_key = f"idem-{uuid4().hex}"
    second_idempotency_key = f"idem-{uuid4().hex}"
    checksum_v1 = "d" * 64
    checksum_v2 = "e" * 64
    first_object_key = build_tenant_document_object_key(tenant_id, document_id)
    second_object_key = f"{tenant_id}/documents/{document_id}/revisions/{uuid4().hex}.pdf"
    first_version_id = uuid5(
        NAMESPACE_URL,
        f"document-ai:{tenant_id}:{document_id}:{first_idempotency_key}",
    )
    second_version_id = uuid5(
        NAMESPACE_URL,
        f"document-ai:{tenant_id}:{document_id}:{second_idempotency_key}",
    )
    session_record = UploadSessionRecord(
        session_id=session_id,
        document_id=document_id,
        session_state="active",
        created_at=_utc_now_iso(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        content_type="application/pdf",
        expected_size_bytes=1024,
        checksum_sha256=checksum_v1,
        expires_at=_utc_future_iso(minutes=15),
        original_filename="original.pdf",
        storage_provider="s3",
        storage_key=first_object_key,
        completed_at=None,
    )
    registry_store.get_document = lambda document_id_value: _load_document_ai_document(
        database_url=database_url,
        document_id=document_id_value,
    )  # type: ignore[method-assign]
    registry_store.set_document = lambda document_record: _store_document_ai_document(
        database_url=database_url,
        document_record=document_record,
    )  # type: ignore[method-assign]

    try:
        first_response = register_upload_completion(
            upload_completion_request=UploadCompletionRequest(
                session_id=session_id,
                object_key=first_object_key,
                checksum_sha256=checksum_v1,
                size_bytes=1024,
                content_type="application/pdf",
            ),
            session_record=session_record,
            principal_user_id=owner_user_id,
            idempotency_key=first_idempotency_key,
            correlation_id="corr-version-first",
            document_registry_store=registry_store,
            source_artifact_store=cast(Any, foundation_store),
        )
        assert first_response.document.document_id == document_id

        second_record = SourceArtifactCreate(
            tenant_id=tenant_id,
            document_version_id=second_version_id,
            storage_key=second_object_key,
            checksum_sha256=checksum_v2,
            content_type="application/pdf",
            size_bytes=2048,
            integrity_state="verified",
            retention_state="active",
        )
        second_artifact = foundation_store.register_source_artifact(
            document_id=document_id,
            record=second_record,
            idempotency_key=second_idempotency_key,
        )
        replay_artifact = foundation_store.register_source_artifact(
            document_id=document_id,
            record=second_record,
            idempotency_key=second_idempotency_key,
        )

        versions = foundation_store.list_document_versions(
            tenant_id=tenant_id, document_id=document_id
        )
        first_version = foundation_store.get_document_version(
            tenant_id=tenant_id, document_version_id=first_version_id
        )
        second_version = foundation_store.get_document_version(
            tenant_id=tenant_id, document_version_id=second_version_id
        )
        first_source = foundation_store.get_source_artifact_for_version(
            tenant_id=tenant_id, document_version_id=first_version_id
        )
        second_source = foundation_store.get_source_artifact_for_version(
            tenant_id=tenant_id, document_version_id=second_version_id
        )

        assert replay_artifact == second_artifact
        assert [item.document_version_id for item in versions] == [first_version_id, second_version_id]
        assert first_version is not None
        assert second_version is not None
        assert first_version.version_number == 1
        assert first_version.version_state == "superseded"
        assert first_version.supersedes_document_version_id is None
        assert second_version.version_number == 2
        assert second_version.version_state == "current"
        assert second_version.supersedes_document_version_id == first_version_id
        assert first_source is not None
        assert second_source is not None
        assert first_source.storage_key == first_object_key
        assert second_source.storage_key == second_object_key
        assert second_source.checksum_sha256 == checksum_v2
        assert second_source.document_id == document_id

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT document_id, active_document_version_id
                    FROM document_ai_documents
                    WHERE tenant_id = %s AND document_id = %s
                    """,
                    (tenant_id, document_id),
                )
                document_row = cursor.fetchone()

        assert document_row is not None
        assert UUID(str(document_row[0])) == document_id
        assert UUID(str(document_row[1])) == second_version_id
    finally:
        _cleanup_document_ai_authority_state(
            database_url=database_url,
            document_id=document_id,
            tenant_id=tenant_id,
            completion_idempotency_key=first_idempotency_key,
        )


def test_upload_completion_atomically_completes_session_with_live_s3_verification(
    monkeypatch: pytest.MonkeyPatch,
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    monkeypatch.setenv("DOCUMENT_AI_RUNTIME_MODE", "production")
    monkeypatch.setenv("DOCUMENT_AI_STORAGE_PROVIDER", "s3")
    if not _live_s3_enabled():
        pytest.skip("Real Document AI S3 configuration is not available.")

    storage_adapter = build_runtime_storage_adapter()
    assert isinstance(storage_adapter, S3StorageAdapter)

    upload_session_store = PersistentUploadSessionStore(database_url=database_url)
    registry_store = PersistentDocumentRegistryStore(database_url=database_url)
    tenant_id = f"tenant-{uuid4().hex[:8]}"
    owner_user_id = uuid4()
    file_bytes = b"live document ai completion verification"
    content_type = "text/plain"
    checksum_sha256 = sha256(file_bytes).hexdigest()
    object_key: str | None = None
    session_id: UUID | None = None
    document_id: UUID | None = None
    idempotency_key = f"idem-{uuid4().hex}"

    try:
        session_response = build_upload_session(
            upload_session_request=UploadSessionCreateRequest(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                file_name="verification.txt",
                content_type=content_type,
                expected_size_bytes=len(file_bytes),
                checksum_sha256=checksum_sha256,
            ),
            principal_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            correlation_id="corr-live-s3-upload-session",
            upload_session_store=upload_session_store,
            storage_adapter=cast(Any, storage_adapter),
        )
        session_id = session_response.session_id
        document_id = session_response.document_id
        object_key = build_tenant_document_object_key(tenant_id, document_id)
        storage_adapter.store_upload_object(object_key, file_bytes, content_type)

        session_record = upload_session_store.get_session(session_response.session_id)
        assert session_record is not None

        completion_response = register_durable_upload_confirmation(
            upload_completion_request=UploadCompletionRequest(
                session_id=session_response.session_id,
                object_key=object_key,
                checksum_sha256=checksum_sha256,
                size_bytes=len(file_bytes),
                content_type=content_type,
            ),
            session_record=session_record,
            principal_user_id=owner_user_id,
            idempotency_key=f"{idempotency_key}-completion",
            correlation_id="corr-live-s3-upload-completion",
            document_registry_store=registry_store,
        )
        replay = register_durable_upload_confirmation(
            upload_completion_request=UploadCompletionRequest(
                session_id=session_response.session_id,
                object_key=object_key,
                checksum_sha256=checksum_sha256,
                size_bytes=len(file_bytes),
                content_type=content_type,
            ),
            session_record=upload_session_store.get_session(session_response.session_id)
            or session_record,
            principal_user_id=owner_user_id,
            idempotency_key=f"{idempotency_key}-completion",
            correlation_id="corr-live-s3-upload-completion-replay",
            document_registry_store=registry_store,
        )

        assert completion_response.document.document_id == document_id
        assert completion_response.processing_operation_id is not None
        assert replay.model_dump(mode="json") == completion_response.model_dump(mode="json")
        assert storage_adapter.object_exists(object_key) is True

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT session_state, completed_at
                    FROM document_ai_upload_sessions
                    WHERE session_id = %s
                    """,
                    (session_response.session_id,),
                )
                session_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_ai_processing_outbox
                    WHERE tenant_id = %s
                    """,
                    (tenant_id,),
                )
                outbox_row = cursor.fetchone()

        assert session_row is not None
        assert str(session_row[0]) == "completed"
        assert session_row[1] is not None
        assert outbox_row == (1,)
    finally:
        if object_key is not None:
            storage_adapter.delete_object(object_key)
        if document_id is not None:
            _cleanup_document_ai_authority_state(
                database_url=database_url,
                document_id=document_id,
                tenant_id=tenant_id,
                session_id=session_id,
                completion_idempotency_key=f"{idempotency_key}-completion",
            )


def test_document_binding_scope_and_signed_access_usage_are_tenant_scoped(
    cockroach_document_ai_database: str,
) -> None:
    database_url = cockroach_document_ai_database
    registry_store = PersistentDocumentRegistryStore(database_url=database_url)
    foundation_store = PersistentDocumentFoundationStore(database_url=database_url)
    binding_store = PersistentDocumentBindingStore(database_url=database_url)
    signed_access_store = PersistentSignedAccessStore(database_url=database_url)
    tenant_id = f"tenant-{uuid4().hex[:8]}"
    other_tenant_id = f"tenant-{uuid4().hex[:8]}"
    owner_user_id = uuid4()
    other_owner_user_id = uuid4()
    document_id = uuid4()
    other_document_id = uuid4()
    capability_id = f"cap-{uuid4().hex}"

    document_record = PersistedDocumentRecord(
        document_id=document_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        state="uploaded",
        storage_key=build_tenant_document_object_key(tenant_id, document_id),
        uploaded_at=_utc_now_iso(),
        checksum_sha256="c" * 64,
        size_bytes=512,
        content_type="application/pdf",
        display_name="binding source",
    )
    other_document_record = PersistedDocumentRecord(
        document_id=other_document_id,
        tenant_id=other_tenant_id,
        owner_user_id=other_owner_user_id,
        state="uploaded",
        storage_key=build_tenant_document_object_key(other_tenant_id, other_document_id),
        uploaded_at=_utc_now_iso(),
        checksum_sha256="d" * 64,
        size_bytes=256,
        content_type="application/pdf",
        display_name="other binding source",
    )
    registry_store.set_document = lambda document_record_value: _store_document_ai_document(
        database_url=database_url,
        document_record=document_record_value,
    )  # type: ignore[method-assign]
    try:
        registry_store.set_document(document_record)
        registry_store.set_document(other_document_record)
        version_id = foundation_store.create_document_version(
            DocumentVersionCreate(
                tenant_id=tenant_id,
                document_id=document_id,
                version_number=1,
                version_state="current",
            )
        )
        foundation_store.create_document_version(
            DocumentVersionCreate(
                tenant_id=other_tenant_id,
                document_id=other_document_id,
                version_number=1,
                version_state="current",
            )
        )

        binding_request = DocumentBindingRequest.model_validate(
            {
                "document_id": document_id,
                "document_version_id": version_id,
                "binding_role": "current_turn_attachment",
                "conversation_id": "conversation-1",
                "turn_id": "turn-1",
                "attachment_order": 0,
            }
        )
        binding = binding_store.create(
            tenant_id=tenant_id,
            actor_user_id=owner_user_id,
            request=binding_request,
            correlation_id="corr-binding",
        )
        replay = binding_store.create(
            tenant_id=tenant_id,
            actor_user_id=owner_user_id,
            request=binding_request,
            correlation_id="corr-binding-replay",
        )
        conflict_request = binding_request.model_copy(update={"document_version_id": uuid4()})

        assert replay.document_binding_id == binding.document_binding_id
        assert binding.document_version_id == version_id
        assert [
            item.document_binding_id
            for item in binding_store.list_for_target(
                tenant_id=tenant_id,
                actor_user_id=owner_user_id,
                conversation_id="conversation-1",
                turn_id="turn-1",
                workflow_id=None,
            )
        ] == [binding.document_binding_id]

        with pytest.raises(DocumentBindingConflictError):
            binding_store.create(
                tenant_id=tenant_id,
                actor_user_id=owner_user_id,
                request=conflict_request,
                correlation_id="corr-binding-conflict",
            )

        signed_access_store.mark_consumed(capability_id)
        signed_access_store.mark_consumed(capability_id)
        assert signed_access_store.is_consumed(capability_id)

        with connect_document_ai_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT document_binding_id, tenant_id, document_id, document_version_id,
                           binding_role, conversation_id, turn_id, attachment_order
                    FROM document_ai_document_bindings
                    WHERE tenant_id = %s AND document_id = %s
                    """,
                    (tenant_id, document_id),
                )
                binding_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT capability_id, consumed_at
                    FROM document_ai_signed_access_usage
                    WHERE capability_id = %s
                    """,
                    (capability_id,),
                )
                capability_row = cursor.fetchone()

        assert binding_row is not None
        assert UUID(str(binding_row[0])) == binding.document_binding_id
        assert str(binding_row[4]) == "current_turn_attachment"
        assert str(binding_row[5]) == "conversation-1"
        assert str(binding_row[6]) == "turn-1"
        assert int(binding_row[7]) == 0

        assert capability_row is not None
        assert str(capability_row[0]) == capability_id
        assert isinstance(capability_row[1], datetime)
    finally:
        _cleanup_document_ai_authority_state(
            database_url=database_url,
            document_id=document_id,
            tenant_id=tenant_id,
            capability_id=capability_id,
        )
        _cleanup_document_ai_authority_state(
            database_url=database_url,
            document_id=other_document_id,
            tenant_id=other_tenant_id,
        )


def _cleanup_document_ai_authority_state(
    *,
    database_url: str,
    document_id: UUID | None = None,
    tenant_id: str | None = None,
    session_id: UUID | None = None,
    completion_idempotency_key: str | None = None,
    capability_id: str | None = None,
) -> None:
    with connect_document_ai_database(database_url) as connection:
        with connection.cursor() as cursor:
            if tenant_id is not None and document_id is not None:
                cursor.execute(
                    """
                    DELETE FROM document_ai_processing_outbox
                    WHERE tenant_id = %s AND processing_operation_id IN (
                        SELECT processing_operation_id
                        FROM document_ai_processing_operations
                        WHERE tenant_id = %s AND document_version_id IN (
                            SELECT document_version_id
                            FROM document_ai_document_versions
                            WHERE tenant_id = %s AND document_id = %s
                        )
                    )
                    """,
                    (tenant_id, tenant_id, tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_processing_work_items
                    WHERE tenant_id = %s AND processing_operation_id IN (
                        SELECT processing_operation_id
                        FROM document_ai_processing_operations
                        WHERE tenant_id = %s AND document_version_id IN (
                            SELECT document_version_id
                            FROM document_ai_document_versions
                            WHERE tenant_id = %s AND document_id = %s
                        )
                    )
                    """,
                    (tenant_id, tenant_id, tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_processing_operations
                    WHERE tenant_id = %s AND document_version_id IN (
                        SELECT document_version_id
                        FROM document_ai_document_versions
                        WHERE tenant_id = %s AND document_id = %s
                    )
                    """,
                    (tenant_id, tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_document_bindings
                    WHERE tenant_id = %s AND document_id = %s
                    """,
                    (tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_source_artifacts
                    WHERE tenant_id = %s AND document_version_id IN (
                        SELECT document_version_id
                        FROM document_ai_document_versions
                        WHERE tenant_id = %s AND document_id = %s
                    )
                    """,
                    (tenant_id, tenant_id, document_id),
                )
                cursor.execute(
                    """
                    UPDATE document_ai_documents
                    SET active_document_version_id = NULL
                    WHERE document_id = %s
                    """,
                    (document_id,),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_document_versions
                    WHERE tenant_id = %s AND document_id = %s
                    """,
                    (tenant_id, document_id),
                )
                cursor.execute(
                    """
                    DELETE FROM document_ai_documents
                    WHERE document_id = %s
                    """,
                    (document_id,),
                )
            if completion_idempotency_key is not None:
                cursor.execute(
                    """
                    DELETE FROM document_ai_completion_idempotency
                    WHERE idempotency_key = %s
                    """,
                    (completion_idempotency_key,),
                )
            if session_id is not None:
                cursor.execute(
                    """
                    DELETE FROM document_ai_upload_sessions
                    WHERE session_id = %s
                    """,
                    (session_id,),
                )
            if capability_id is not None:
                cursor.execute(
                    """
                    DELETE FROM document_ai_signed_access_usage
                    WHERE capability_id = %s
                    """,
                    (capability_id,),
                )
        connection.commit()


def _parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_iso_datetime_or_none(value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_iso_datetime(value)


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _utc_future_iso(*, minutes: int) -> str:
    return (datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=minutes)).isoformat()


def _live_s3_enabled() -> bool:
    bucket = get_document_ai_s3_bucket()
    return (
        get_document_ai_runtime_mode() == "production"
        and get_document_ai_storage_provider() == "s3"
        and isinstance(bucket, str)
        and bucket.strip() != ""
        and not bucket.strip().startswith("<")
    )
