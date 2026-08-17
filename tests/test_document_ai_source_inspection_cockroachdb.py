"""Live CockroachDB and real-storage regressions for document source inspection."""

from __future__ import annotations

from uuid import uuid4
from hashlib import sha256
from pathlib import Path

from dotenv import load_dotenv
import pytest
import psycopg

from services.document_ai.app.config import get_document_ai_runtime_mode
from services.document_ai.app.config import get_document_ai_storage_provider
from services.document_ai.app.storage_keys import build_tenant_document_object_key
from services.document_ai.app.storage_adapter import build_runtime_storage_adapter
from services.document_ai.app.upload_sessions import build_upload_session
from services.document_ai.app.upload_sessions import UploadSessionCreateRequest
from services.document_ai.app.upload_sessions import PersistentUploadSessionStore
from services.document_ai.app.document_registry import UploadCompletionRequest
from services.document_ai.app.document_registry import register_upload_completion
from services.document_ai.app.document_registry import PersistentDocumentRegistryStore
from services.document_ai.app.processing_workers import ProcessingWorkerRepository
from services.document_ai.migrations.cockroachdb import runner
from services.document_ai.app.document_foundation import PersistentDocumentFoundationStore
from services.document_ai.app.persistence_support import load_document_ai_database_url
from services.document_ai.app.processing_work_discovery import ProcessingWorkDiscoveryRepository
from services.document_ai.app.source_inspection_service import SourceInspectionRepository

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
    return database_url


def test_source_inspection_persists_and_replays_normalized_family_metadata(
    cockroach_document_ai_database: str,
) -> None:
    _skip_if_real_s3_unavailable()
    database_url = cockroach_document_ai_database
    storage = _CountingStorageAdapter(build_runtime_storage_adapter())
    session_store = PersistentUploadSessionStore(database_url=database_url)
    registry_store = PersistentDocumentRegistryStore(database_url=database_url)
    foundation_store = PersistentDocumentFoundationStore(database_url=database_url)
    repository = SourceInspectionRepository(database_url=database_url)

    tenant_id = f"tenant-{uuid4().hex[:8]}"
    document_id = uuid4()
    owner_user_id = uuid4()
    idempotency_key = f"idem-{uuid4().hex}"
    payload = Path("tests/fixtures/document_ai/invoice_sample.pdf").read_bytes()
    checksum_sha256 = sha256(payload).hexdigest()
    object_key = build_tenant_document_object_key(tenant_id, document_id)

    storage.store_upload_object(object_key, payload, "application/pdf")
    request = UploadSessionCreateRequest(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        file_name="invoice.pdf",
        content_type="application/pdf",
        expected_size_bytes=len(payload),
        checksum_sha256=checksum_sha256,
    )
    response = build_upload_session(
        upload_session_request=request,
        principal_user_id=owner_user_id,
        idempotency_key=idempotency_key,
        correlation_id=f"corr-{uuid4().hex}",
        upload_session_store=session_store,
        storage_adapter=storage.inner,
    )
    session_record = session_store.get_session(response.session_id)
    assert session_record is not None

    register_upload_completion(
        upload_completion_request=UploadCompletionRequest(
            session_id=response.session_id,
            object_key=object_key,
            checksum_sha256=checksum_sha256,
            size_bytes=len(payload),
            content_type="application/pdf",
        ),
        session_record=session_record,
        principal_user_id=owner_user_id,
        idempotency_key=f"complete-{uuid4().hex}",
        correlation_id=f"corr-{uuid4().hex}",
        document_registry_store=registry_store,
        source_artifact_store=foundation_store,
    )

    candidate = ProcessingWorkDiscoveryRepository(
        database_url=database_url
    ).discover_work_candidates(limit=1)[0]
    lease = ProcessingWorkerRepository(database_url=database_url).claim_candidate(
        candidate=candidate,
        worker_id="worker-a",
    )
    assert lease is not None

    result = repository.inspect_operation(lease=lease.to_lease(), storage=storage)
    replay = repository.inspect_operation(lease=lease.to_lease(), storage=storage)

    assert result.model_dump(mode="json") == replay.model_dump(mode="json")
    assert result.observed_source_family == "pdf"
    assert result.observed_source_format == "pdf"
    assert result.source_size_bytes == len(payload)
    assert result.diagnostic_payload["source_size_bytes"] == len(payload)
    assert storage.get_object_metadata_calls == 1
    assert storage.resolve_download_object_calls == 1

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT observed_source_family, observed_source_format, source_size_bytes,
                       diagnostic_payload
                FROM document_ai_source_inspections
                WHERE tenant_id = %s AND processing_operation_id = %s
                """,
                (tenant_id, lease.processing_operation_id),
            )
            row = cursor.fetchone()

    assert row is not None
    assert str(row[0]) == "pdf"
    assert str(row[1]) == "pdf"
    assert int(row[2]) == len(payload)
    assert row[3]["observed_source_family"] == "pdf"

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT scope_kind, scope_ordinal, parent_structural_scope_id
                FROM document_ai_structural_scopes
                WHERE tenant_id = %s AND processing_operation_id = %s
                ORDER BY scope_ordinal ASC
                """,
                (tenant_id, lease.processing_operation_id),
            )
            scope_rows = cursor.fetchall()

    assert scope_rows
    assert str(scope_rows[0][0]) == "document"
    assert int(scope_rows[0][1]) == 0
    assert scope_rows[0][2] is None


def test_source_inspection_rejects_malformed_content_and_stale_fences(
    cockroach_document_ai_database: str,
) -> None:
    _skip_if_real_s3_unavailable()
    database_url = cockroach_document_ai_database
    storage = _CountingStorageAdapter(build_runtime_storage_adapter())
    session_store = PersistentUploadSessionStore(database_url=database_url)
    registry_store = PersistentDocumentRegistryStore(database_url=database_url)
    foundation_store = PersistentDocumentFoundationStore(database_url=database_url)
    repository = SourceInspectionRepository(database_url=database_url)

    tenant_id = f"tenant-{uuid4().hex[:8]}"
    document_id = uuid4()
    owner_user_id = uuid4()
    idempotency_key = f"idem-{uuid4().hex}"
    payload = b"PK\x03\x04archive"
    checksum_sha256 = sha256(payload).hexdigest()
    object_key = build_tenant_document_object_key(tenant_id, document_id)

    storage.store_upload_object(object_key, payload, "application/pdf")
    request = UploadSessionCreateRequest(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        file_name="broken.pdf",
        content_type="application/pdf",
        expected_size_bytes=len(payload),
        checksum_sha256=checksum_sha256,
    )
    response = build_upload_session(
        upload_session_request=request,
        principal_user_id=owner_user_id,
        idempotency_key=idempotency_key,
        correlation_id=f"corr-{uuid4().hex}",
        upload_session_store=session_store,
        storage_adapter=storage.inner,
    )
    session_record = session_store.get_session(response.session_id)
    assert session_record is not None

    register_upload_completion(
        upload_completion_request=UploadCompletionRequest(
            session_id=response.session_id,
            object_key=object_key,
            checksum_sha256=checksum_sha256,
            size_bytes=len(payload),
            content_type="application/pdf",
        ),
        session_record=session_record,
        principal_user_id=owner_user_id,
        idempotency_key=f"complete-{uuid4().hex}",
        correlation_id=f"corr-{uuid4().hex}",
        document_registry_store=registry_store,
        source_artifact_store=foundation_store,
    )

    candidate = ProcessingWorkDiscoveryRepository(
        database_url=database_url
    ).discover_work_candidates(limit=1)[0]
    lease = ProcessingWorkerRepository(database_url=database_url).claim_candidate(
        candidate=candidate,
        worker_id="worker-a",
    )
    assert lease is not None

    result = repository.inspect_operation(lease=lease.to_lease(), storage=storage)
    assert result.reason == "archive_not_permitted"
    assert result.disposition == "quarantined"
    assert storage.get_object_metadata_calls == 1
    assert storage.resolve_download_object_calls == 1

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE document_ai_processing_work_items
                SET fencing_token = fencing_token + 1,
                    current_processing_attempt_id = NULL,
                    state = 'queued',
                    leased_until = NULL
                WHERE tenant_id = %s AND processing_work_item_id = %s
                """,
                (tenant_id, lease.processing_work_item_id),
            )
        connection.commit()

    with pytest.raises(ValueError, match="source_inspection_operation_not_found"):
        repository.inspect_operation(lease=lease.to_lease(), storage=storage)


def test_source_inspection_persists_structural_scopes_for_text_sources(
    cockroach_document_ai_database: str,
) -> None:
    _skip_if_real_s3_unavailable()
    database_url = cockroach_document_ai_database
    storage = _CountingStorageAdapter(build_runtime_storage_adapter())
    session_store = PersistentUploadSessionStore(database_url=database_url)
    registry_store = PersistentDocumentRegistryStore(database_url=database_url)
    foundation_store = PersistentDocumentFoundationStore(database_url=database_url)
    repository = SourceInspectionRepository(database_url=database_url)

    tenant_id = f"tenant-{uuid4().hex[:8]}"
    document_id = uuid4()
    owner_user_id = uuid4()
    idempotency_key = f"idem-{uuid4().hex}"
    payload = "\n".join(f"line-{index}" for index in range(1, 121)).encode("utf-8")
    checksum_sha256 = sha256(payload).hexdigest()
    object_key = build_tenant_document_object_key(tenant_id, document_id)

    storage.store_upload_object(object_key, payload, "text/plain")
    request = UploadSessionCreateRequest(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        file_name="notes.txt",
        content_type="text/plain",
        expected_size_bytes=len(payload),
        checksum_sha256=checksum_sha256,
    )
    response = build_upload_session(
        upload_session_request=request,
        principal_user_id=owner_user_id,
        idempotency_key=idempotency_key,
        correlation_id=f"corr-{uuid4().hex}",
        upload_session_store=session_store,
        storage_adapter=storage.inner,
    )
    session_record = session_store.get_session(response.session_id)
    assert session_record is not None

    register_upload_completion(
        upload_completion_request=UploadCompletionRequest(
            session_id=response.session_id,
            object_key=object_key,
            checksum_sha256=checksum_sha256,
            size_bytes=len(payload),
            content_type="text/plain",
        ),
        session_record=session_record,
        principal_user_id=owner_user_id,
        idempotency_key=f"complete-{uuid4().hex}",
        correlation_id=f"corr-{uuid4().hex}",
        document_registry_store=registry_store,
        source_artifact_store=foundation_store,
    )

    candidate = ProcessingWorkDiscoveryRepository(
        database_url=database_url
    ).discover_work_candidates(limit=1)[0]
    lease = ProcessingWorkerRepository(database_url=database_url).claim_candidate(
        candidate=candidate,
        worker_id="worker-a",
    )
    assert lease is not None

    result = repository.inspect_operation(lease=lease.to_lease(), storage=storage)
    replay = repository.inspect_operation(lease=lease.to_lease(), storage=storage)

    assert result.model_dump(mode="json") == replay.model_dump(mode="json")
    assert result.observed_source_family == "text"
    assert result.observed_source_format == "plain"

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT scope_kind, scope_ordinal, structural_coordinates
                FROM document_ai_structural_scopes
                WHERE tenant_id = %s AND processing_operation_id = %s
                ORDER BY scope_ordinal ASC
                """,
                (tenant_id, lease.processing_operation_id),
            )
            scope_rows = cursor.fetchall()

    assert len(scope_rows) == 4
    assert [str(row[0]) for row in scope_rows] == [
        "document",
        "line_range",
        "line_range",
        "line_range",
    ]
    assert [int(row[1]) for row in scope_rows] == [0, 1, 2, 3]
    assert scope_rows[1][2]["start_line"] == 1
    assert scope_rows[1][2]["end_line"] == 50
    assert scope_rows[3][2]["start_line"] == 101
    assert scope_rows[3][2]["end_line"] == 120


def test_source_inspection_persists_provider_partitions_for_large_text_sources(
    cockroach_document_ai_database: str,
) -> None:
    _skip_if_real_s3_unavailable()
    database_url = cockroach_document_ai_database
    storage = _CountingStorageAdapter(build_runtime_storage_adapter())
    session_store = PersistentUploadSessionStore(database_url=database_url)
    registry_store = PersistentDocumentRegistryStore(database_url=database_url)
    foundation_store = PersistentDocumentFoundationStore(database_url=database_url)
    repository = SourceInspectionRepository(database_url=database_url)

    tenant_id = f"tenant-{uuid4().hex[:8]}"
    document_id = uuid4()
    owner_user_id = uuid4()
    idempotency_key = f"idem-{uuid4().hex}"
    payload = "\n".join(
        f"{index}:{'x' * 70000}" for index in range(1, 121)
    ).encode("utf-8")
    checksum_sha256 = sha256(payload).hexdigest()
    object_key = build_tenant_document_object_key(tenant_id, document_id)

    storage.store_upload_object(object_key, payload, "text/plain")
    request = UploadSessionCreateRequest(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        file_name="large-notes.txt",
        content_type="text/plain",
        expected_size_bytes=len(payload),
        checksum_sha256=checksum_sha256,
    )
    response = build_upload_session(
        upload_session_request=request,
        principal_user_id=owner_user_id,
        idempotency_key=idempotency_key,
        correlation_id=f"corr-{uuid4().hex}",
        upload_session_store=session_store,
        storage_adapter=storage.inner,
    )
    session_record = session_store.get_session(response.session_id)
    assert session_record is not None

    register_upload_completion(
        upload_completion_request=UploadCompletionRequest(
            session_id=response.session_id,
            object_key=object_key,
            checksum_sha256=checksum_sha256,
            size_bytes=len(payload),
            content_type="text/plain",
        ),
        session_record=session_record,
        principal_user_id=owner_user_id,
        idempotency_key=f"complete-{uuid4().hex}",
        correlation_id=f"corr-{uuid4().hex}",
        document_registry_store=registry_store,
        source_artifact_store=foundation_store,
    )

    candidate = ProcessingWorkDiscoveryRepository(
        database_url=database_url
    ).discover_work_candidates(limit=1)[0]
    lease = ProcessingWorkerRepository(database_url=database_url).claim_candidate(
        candidate=candidate,
        worker_id="worker-a",
    )
    assert lease is not None

    result = repository.inspect_operation(lease=lease.to_lease(), storage=storage)
    assert result.observed_source_family == "text"
    assert result.observed_source_format == "plain"

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT source_inspection_id
                FROM document_ai_source_inspections
                WHERE tenant_id = %s AND processing_operation_id = %s
                """,
                (tenant_id, lease.processing_operation_id),
            )
            inspection_row = cursor.fetchone()
            assert inspection_row is not None
            source_inspection_id = inspection_row[0]

            cursor.execute(
                """
                SELECT partition_ordinal, partition_kind, structural_coordinates,
                       partition_payload, partition_identity
                FROM document_ai_provider_partitions
                WHERE tenant_id = %s AND source_inspection_id = %s
                ORDER BY partition_ordinal ASC
                """,
                (tenant_id, source_inspection_id),
            )
            partition_rows = cursor.fetchall()

    assert len(partition_rows) >= 2
    assert [int(row[0]) for row in partition_rows] == list(range(len(partition_rows)))
    assert all(int(row[3]["estimated_input_bytes"]) <= 8 * 1024 * 1024 for row in partition_rows)
    assert sum(int(row[3]["unit_count"]) for row in partition_rows) == 120
    assert all(str(row[4]) for row in partition_rows)


def _skip_if_real_s3_unavailable() -> None:
    if get_document_ai_runtime_mode() != "production":
        pytest.skip("Real S3 integration requires production runtime settings.")
    if get_document_ai_storage_provider() != "s3":
        pytest.skip("Real S3 integration requires DOCUMENT_AI_STORAGE_PROVIDER=s3.")


class _CountingStorageAdapter:
    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.get_object_metadata_calls = 0
        self.resolve_download_object_calls = 0

    def get_object_metadata(self, object_key: str) -> object:
        self.get_object_metadata_calls += 1
        return self.inner.get_object_metadata(object_key)  # type: ignore[attr-defined]

    def resolve_download_object(self, object_key: str) -> tuple[Path, str]:
        self.resolve_download_object_calls += 1
        return self.inner.resolve_download_object(object_key)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)
