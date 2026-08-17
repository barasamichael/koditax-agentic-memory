"""Live CockroachDB coverage for canonical candidate validation."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from typing import cast
from hashlib import sha256
from pathlib import Path

from dotenv import load_dotenv
import pytest
import psycopg

from services.document_ai.app.config import get_document_ai_runtime_mode
from services.document_ai.app.config import get_document_ai_storage_provider
from services.document_ai.app.storage_keys import build_tenant_document_object_key
from services.document_ai.app.governed_openai import OpenAIProviderError
from services.document_ai.app.governed_openai import GovernedOpenAIClient
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
from services.document_ai.app.canonical_activation import CanonicalActivationRepository
from services.document_ai.app.canonical_activation import CanonicalActivationWorkExecutor
from services.document_ai.app.canonical_validation import CanonicalValidationRepository
from services.document_ai.app.processing_work_discovery import ProcessingWorkDiscoveryRepository
from services.document_ai.app.source_inspection_service import SourceInspectionRepository
from services.document_ai.app.provider_result_repository import ProviderResultRepository
from services.document_ai.app.openai_document_understanding import OpenAIUnderstandingRepository
from services.document_ai.app.openai_document_understanding import OpenAIUnderstandingWorkExecutor

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


def test_real_canonical_candidate_is_validated_and_keeps_retrieval_artifacts_uncreated(
    cockroach_document_ai_database: str,
) -> None:
    _skip_if_live_dependencies_unavailable()
    database_url = cockroach_document_ai_database
    tenant_id, canonical_representation_id = _build_real_candidate(database_url=database_url)

    validation_repository = CanonicalValidationRepository(database_url=database_url)
    result = validation_repository.validate_canonical_representation(
        tenant_id=tenant_id, canonical_representation_id=canonical_representation_id
    )

    assert result.state == "validated"
    assert result.readiness == "full"
    assert result.reasons == ()

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT representation.state, representation.readiness_state,
                       representation.canonical_validation_version,
                       representation.validation_report, representation.validated_at
                  FROM document_ai_canonical_representations AS representation
                 WHERE representation.tenant_id = %s
                   AND representation.canonical_representation_id = %s
                """,
                (tenant_id, canonical_representation_id),
            )
            row = cursor.fetchone()
            assert row is not None
            assert str(row[0]) == "validated"
            assert str(row[1]) == "full"
            assert str(row[2]) == "v1"
            assert isinstance(row[3], dict)
            assert row[3]["reason_codes"] == []
            assert row[4] is not None

            cursor.execute(
                """
                SELECT event_type
                  FROM document_ai_processing_outbox
                 WHERE tenant_id = %s
                   AND processing_operation_id = (
                       SELECT processing_operation_id
                         FROM document_ai_canonical_representations
                        WHERE tenant_id = %s
                          AND canonical_representation_id = %s
                   )
                """,
                (tenant_id, tenant_id, canonical_representation_id),
            )
            assert "canonical_chunking_requested" in {
                str(row[0]) for row in cursor.fetchall()
            }

            cursor.execute(
                """
                SELECT COUNT(*)
                  FROM document_ai_retrieval_chunks
                 WHERE tenant_id = %s
                   AND canonical_representation_id = %s
                """,
                (tenant_id, canonical_representation_id),
            )
            assert int(cursor.fetchone()[0]) == 0

            cursor.execute(
                """
                SELECT COUNT(*)
                  FROM document_ai_chunk_embeddings
                 WHERE tenant_id = %s
                   AND canonical_representation_id = %s
                """,
                (tenant_id, canonical_representation_id),
            )
            assert int(cursor.fetchone()[0]) == 0


def test_tampered_real_candidate_is_rejected(
    cockroach_document_ai_database: str,
) -> None:
    _skip_if_live_dependencies_unavailable()
    database_url = cockroach_document_ai_database
    tenant_id, canonical_representation_id = _build_real_candidate(database_url=database_url)

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE document_ai_canonical_elements
                   SET reading_order = 99
                 WHERE tenant_id = %s
                   AND canonical_representation_id = %s
                   AND ordinal = 0
                """,
                (tenant_id, canonical_representation_id),
            )
        connection.commit()

    validation_repository = CanonicalValidationRepository(database_url=database_url)
    result = validation_repository.validate_canonical_representation(
        tenant_id=tenant_id, canonical_representation_id=canonical_representation_id
    )

    assert result.state == "rejected"
    assert result.readiness == "none"
    assert "canonical_reading_order_invalid" in result.reasons

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT representation.state, representation.readiness_state,
                       representation.validation_report
                  FROM document_ai_canonical_representations AS representation
                 WHERE representation.tenant_id = %s
                   AND representation.canonical_representation_id = %s
                """,
                (tenant_id, canonical_representation_id),
            )
            row = cursor.fetchone()
            assert row is not None
            assert str(row[0]) == "rejected"
            assert str(row[1]) == "none"
            assert "canonical_reading_order_invalid" in cast(
                dict[str, object], row[2]
            )["reason_codes"]


def _build_real_candidate(*, database_url: str) -> tuple[str, UUID]:
    storage = _CountingStorageAdapter(build_runtime_storage_adapter())
    session_store = PersistentUploadSessionStore(database_url=database_url)
    registry_store = PersistentDocumentRegistryStore(database_url=database_url)
    foundation_store = PersistentDocumentFoundationStore(database_url=database_url)
    inspection_repository = SourceInspectionRepository(database_url=database_url)
    understanding_repository = OpenAIUnderstandingRepository(database_url=database_url)
    result_repository = ProviderResultRepository(database_url=database_url)
    canonical_repository = CanonicalActivationRepository(database_url=database_url)
    client = _build_live_client()

    tenant_id = f"tenant-{uuid4().hex[:8]}"
    document_id = uuid4()
    owner_user_id = uuid4()
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
        idempotency_key=f"idem-{uuid4().hex}",
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

    inspection_lease = _claim_work_item(database_url=database_url, work_kind="source_inspection")
    inspection = inspection_repository.inspect_operation(
        lease=inspection_lease.to_lease(),
        storage=storage,
    )
    assert inspection.accepted_for_processing

    understanding_lease = _claim_work_item(
        database_url=database_url,
        work_kind="general_document_understanding",
    )
    understanding_executor = OpenAIUnderstandingWorkExecutor(
        repository=understanding_repository,
        result_repository=result_repository,
        storage=storage,
        client=client,
    )
    result_reference = understanding_executor.execute(
        lease=understanding_lease.to_lease(),
        checkpoint=None,
    )
    assert result_reference.startswith("provider-result:")

    canonical_lease = _claim_work_item(database_url=database_url, work_kind="canonical_assembly")
    canonical_executor = CanonicalActivationWorkExecutor(repository=canonical_repository)
    canonical_reference = canonical_executor.execute(
        lease=canonical_lease.to_lease(),
        checkpoint=None,
    )
    assert canonical_reference.startswith("canonical-representation:")
    return tenant_id, UUID(canonical_reference.split(":", 1)[1])


def _skip_if_live_dependencies_unavailable() -> None:
    if get_document_ai_runtime_mode() != "production":
        pytest.skip("Real OpenAI execution requires production runtime settings.")
    if get_document_ai_storage_provider() != "s3":
        pytest.skip("Real OpenAI execution requires DOCUMENT_AI_STORAGE_PROVIDER=s3.")
    try:
        GovernedOpenAIClient.from_environment()
    except OpenAIProviderError as error:
        if error.reason == "missing_openai_configuration":
            pytest.skip("OpenAI document processing is not configured in the environment.")
        raise


def _build_live_client() -> GovernedOpenAIClient:
    return GovernedOpenAIClient.from_environment()


def _claim_work_item(*, database_url: str, work_kind: str):
    repository = ProcessingWorkerRepository(database_url=database_url, lease_seconds=60)
    candidates = ProcessingWorkDiscoveryRepository(
        database_url=database_url,
    ).discover_work_candidates(limit=10)
    candidate = next(candidate for candidate in candidates if candidate.work_kind == work_kind)
    lease = repository.claim_candidate(candidate=candidate, worker_id=f"worker-{work_kind}")
    assert lease is not None
    return lease


class _CountingStorageAdapter:
    def __init__(self, inner: object) -> None:
        self.inner = inner

    def store_upload_object(self, object_key: str, payload: bytes, content_type: str) -> None:
        self.inner.store_upload_object(object_key, payload, content_type)  # type: ignore[attr-defined]

    def resolve_download_object(self, object_key: str):
        return self.inner.resolve_download_object(object_key)  # type: ignore[attr-defined]

    def get_object_metadata(self, object_key: str):
        return self.inner.get_object_metadata(object_key)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)
