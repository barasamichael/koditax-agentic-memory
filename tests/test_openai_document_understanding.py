"""Focused durable-executor tests for Milestone 15 OpenAI understanding."""

from __future__ import annotations

from uuid import uuid4
from typing import cast
from hashlib import sha256
from pathlib import Path

import pytest

from services.document_ai.app.governed_openai import ProviderUsage
from services.document_ai.app.governed_openai import OpenAIProviderError
from services.document_ai.app.governed_openai import GovernedOpenAIClient
from services.document_ai.app.governed_openai import GovernedOpenAIRequest
from services.document_ai.app.governed_openai import ValidatedProviderResult
from services.document_ai.app.storage_adapter import StorageAdapterProtocol
from services.document_ai.app.processing_workers import ProcessingAttemptLease
from services.document_ai.app.provider_result_repository import ProviderResultRepository
from services.document_ai.app.provider_result_repository import ProviderResultReservation
from services.document_ai.app.provider_result_repository import ProviderResultReservationDetails
from services.document_ai.app.openai_document_understanding import EligibleUnderstandingSource
from services.document_ai.app.openai_document_understanding import OpenAIUnderstandingRepository
from services.document_ai.app.openai_document_understanding import OpenAIUnderstandingWorkExecutor
from services.document_ai.app.openai_document_understanding import (
    EligibleUnderstandingStructuralScope,
)


def _lease() -> ProcessingAttemptLease:
    return ProcessingAttemptLease(
        tenant_id="tenant-a",
        processing_operation_id=uuid4(),
        processing_work_item_id=uuid4(),
        processing_attempt_id=uuid4(),
        worker_id="worker-a",
        fencing_token=1,
        lease_seconds=60,
        correlation_id="correlation-a",
    )


class _Repository:
    def __init__(self, source: EligibleUnderstandingSource, existing: str | None = None) -> None:
        self.source = source
        self.existing = existing
        self.loads = 0

    def existing_result_reference(self, *, lease: ProcessingAttemptLease) -> str | None:
        assert lease.tenant_id == "tenant-a"
        return self.existing

    def load_eligible_source(self, *, lease: ProcessingAttemptLease) -> EligibleUnderstandingSource:
        self.loads += 1
        assert lease.tenant_id == "tenant-a"
        return self.source


class _Storage:
    def __init__(self, source_path: Path, media_type: str = "application/pdf") -> None:
        self.source_path = source_path
        self.media_type = media_type

    def resolve_download_object(self, object_key: str) -> tuple[Path, str]:
        assert object_key == "private-source"
        return self.source_path, self.media_type


class _Client:
    def __init__(self) -> None:
        self.calls = 0

    def understand(self, request: GovernedOpenAIRequest) -> ValidatedProviderResult:
        self.calls += 1
        return ValidatedProviderResult(
            model="gpt-4.1-mini",
            processing_operation_id=request.processing_operation_id,
            processing_attempt_id=request.processing_attempt_id,
            document_version_id=request.source.document_version_id,
            source_scope_id=request.source.source_scope_id,
            processing_policy_version="v1",
            prompt_version="general-document-understanding-v1",
            canonical_schema_version="v1",
            result={"schema_version": "v1", "pages": [], "warnings": []},
            usage=ProviderUsage(),
            latency_ms=1,
        )


class _Results:
    def reserve(
        self,
        *,
        lease: ProcessingAttemptLease,
        details: ProviderResultReservationDetails,
    ) -> ProviderResultReservation:
        assert lease.tenant_id == "tenant-a"
        return ProviderResultReservation(
            reservation_id="reservation:1",
            reservation_state="reserved",
            reservation_generation=1,
            request_fingerprint=details.request_fingerprint,
            result_reference=None,
            provider_request_id=None,
            provider_response_id=None,
            can_call_provider=True,
        )

    def mark_in_progress(
        self,
        *,
        lease: ProcessingAttemptLease,
        reservation: ProviderResultReservation,
    ) -> ProviderResultReservation:
        assert lease.tenant_id == "tenant-a"
        return ProviderResultReservation(
            reservation_id=reservation.reservation_id,
            reservation_state="in_progress",
            reservation_generation=reservation.reservation_generation,
            request_fingerprint=reservation.request_fingerprint,
            result_reference=reservation.result_reference,
            provider_request_id=reservation.provider_request_id,
            provider_response_id=reservation.provider_response_id,
            can_call_provider=reservation.can_call_provider,
        )

    def persist(
        self,
        *,
        lease: ProcessingAttemptLease,
        details: ProviderResultReservationDetails,
        reservation: ProviderResultReservation,
        result: ValidatedProviderResult,
    ) -> str:
        assert result.processing_attempt_id == str(lease.processing_attempt_id)
        assert details.request_fingerprint == reservation.request_fingerprint
        return "provider-result:durable"


def _source(
    size_bytes: int,
    *,
    media_type: str = "application/pdf",
    content: bytes | None = None,
) -> EligibleUnderstandingSource:
    source_bytes = content if content is not None else (
        b"%PDF-1.7\n" if size_bytes == 9 else b"x" * size_bytes
    )
    return EligibleUnderstandingSource(
        document_version_id=uuid4(),
        source_artifact_id=uuid4(),
        storage_key="private-source",
        content_type=media_type,
        size_bytes=size_bytes,
        checksum_sha256=sha256(source_bytes).hexdigest(),
        structural_scopes=(
            EligibleUnderstandingStructuralScope(
                structural_scope_id=uuid4(),
                scope_kind="line_range",
                scope_ordinal=1,
                scope_identity="scope-1",
                structural_coordinates={"kind": "line_range", "start_line": 1, "end_line": 1},
                scope_payload={
                    "scope_kind": "line_range",
                    "scope_label": "line-1-1",
                    "source_family": "text",
                    "source_format": "plain",
                    "start": 1,
                    "end": 1,
                    "scope_size": 1,
                    "window_size": 50,
                },
            ),
        ),
    )


def test_existing_result_prevents_a_repeat_provider_call(tmp_path: Path) -> None:
    repository = _Repository(_source(1), existing="provider-result:existing")
    client = _Client()
    executor = OpenAIUnderstandingWorkExecutor(
        repository=cast(OpenAIUnderstandingRepository, repository),
        result_repository=cast(ProviderResultRepository, _Results()),
        storage=cast(StorageAdapterProtocol, _Storage(tmp_path / "unused")),
        client=cast(GovernedOpenAIClient, client),
    )
    assert executor.execute(lease=_lease(), checkpoint=None) == "provider-result:existing"
    assert client.calls == 0
    assert repository.loads == 0


def test_executor_reloads_gate_and_cleans_temporary_source(tmp_path: Path) -> None:
    content = b"%PDF-1.7\n"
    source_path = tmp_path / "document-ai-download-test.pdf"
    source_path.write_bytes(content)
    source = _source(len(content))
    repository = _Repository(source)
    client = _Client()
    executor = OpenAIUnderstandingWorkExecutor(
        repository=cast(OpenAIUnderstandingRepository, repository),
        result_repository=cast(ProviderResultRepository, _Results()),
        storage=cast(StorageAdapterProtocol, _Storage(source_path)),
        client=cast(GovernedOpenAIClient, client),
    )
    assert executor.execute(lease=_lease(), checkpoint=None) == "provider-result:durable"
    assert repository.loads == 2
    assert client.calls == 1
    assert not source_path.exists()


def test_executor_rejects_mismatched_download_before_provider_call(tmp_path: Path) -> None:
    source_path = tmp_path / "document-ai-download-test.pdf"
    source_path.write_bytes(b"not-the-expected-size")
    client = _Client()
    executor = OpenAIUnderstandingWorkExecutor(
        repository=cast(OpenAIUnderstandingRepository, _Repository(_source(1))),
        result_repository=cast(ProviderResultRepository, _Results()),
        storage=cast(StorageAdapterProtocol, _Storage(source_path)),
        client=cast(GovernedOpenAIClient, client),
    )
    with pytest.raises(OpenAIProviderError) as caught:
        executor.execute(lease=_lease(), checkpoint=None)
    assert caught.value.reason == "provider_input_mismatch"
    assert client.calls == 0


def test_executor_rejects_active_reservation_before_provider_call(tmp_path: Path) -> None:
    content = b"%PDF-1.7\n"
    source_path = tmp_path / "document-ai-download-test.pdf"
    source_path.write_bytes(content)
    source = _source(len(content))
    client = _Client()

    class _BlockedResults(_Results):
        def reserve(
            self,
            *,
            lease: ProcessingAttemptLease,
            details: ProviderResultReservationDetails,
        ) -> ProviderResultReservation:
            del lease
            return ProviderResultReservation(
                reservation_id="reservation:blocked",
                reservation_state="blocked",
                reservation_generation=1,
                request_fingerprint=details.request_fingerprint,
                result_reference=None,
                provider_request_id=None,
                provider_response_id=None,
                can_call_provider=False,
            )

    executor = OpenAIUnderstandingWorkExecutor(
        repository=cast(OpenAIUnderstandingRepository, _Repository(source)),
        result_repository=cast(ProviderResultRepository, _BlockedResults()),
        storage=cast(StorageAdapterProtocol, _Storage(source_path)),
        client=cast(GovernedOpenAIClient, client),
    )

    with pytest.raises(OpenAIProviderError) as caught:
        executor.execute(lease=_lease(), checkpoint=None)

    assert caught.value.reason == "provider_result_reserved"
    assert client.calls == 0


def test_executor_includes_structural_scope_manifest_in_provider_request(
    tmp_path: Path,
) -> None:
    content = b"line 1\nline 2\n"
    source_path = tmp_path / "document-ai-download-test.txt"
    source_path.write_bytes(content)
    source = _source(len(content), media_type="text/plain", content=content)
    repository = _Repository(source)
    transport = _CaptureTransport()
    client = _TransportClient(transport=transport)
    executor = OpenAIUnderstandingWorkExecutor(
        repository=cast(OpenAIUnderstandingRepository, repository),
        result_repository=cast(ProviderResultRepository, _Results()),
        storage=cast(StorageAdapterProtocol, _Storage(source_path, media_type="text/plain")),
        client=cast(GovernedOpenAIClient, client),
    )

    executor.execute(lease=_lease(), checkpoint=None)

    assert transport.payload is not None
    metadata = transport.payload["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["structural_scope_ids"]
    input_file = transport.payload["input"][0]["content"][0]  # type: ignore[index]
    assert input_file["filename"] == "source.txt"
    request_text = transport.payload["input"][0]["content"][1]["text"]  # type: ignore[index]
    assert "structural scopes" in request_text.lower()
    assert "scope-1" in request_text


def test_executor_rejects_stale_worker_after_successful_provider_response(
    tmp_path: Path,
) -> None:
    content = b"%PDF-1.7\n"
    source_path = tmp_path / "document-ai-download-test.pdf"
    source_path.write_bytes(content)
    repository = _Repository(_source(len(content)))
    client = _Client()
    executor = OpenAIUnderstandingWorkExecutor(
        repository=cast(OpenAIUnderstandingRepository, repository),
        result_repository=cast(ProviderResultRepository, _StaleResults()),
        storage=cast(StorageAdapterProtocol, _Storage(source_path)),
        client=cast(GovernedOpenAIClient, client),
    )

    with pytest.raises(OpenAIProviderError) as caught:
        executor.execute(lease=_lease(), checkpoint=None)

    assert caught.value.reason == "stale_understanding_result"
    assert client.calls == 1


class _CaptureTransport:
    def __init__(self) -> None:
        self.payload: dict[str, object] | None = None

    def create(self, **payload: object) -> object:
        self.payload = payload
        return type(
            "_Response",
            (),
            {
                "id": "resp-1",
                "output_text": (
                    '{"result":{"schema_version":"v1","pages":[],"warnings":[]}}'
                ),
                "usage": type(
                    "_Usage",
                    (),
                    {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )(),
            },
        )()


class _TransportClient:
    def __init__(self, *, transport: _CaptureTransport) -> None:
        self.calls = 0
        self._transport = transport

    def understand(self, request: GovernedOpenAIRequest) -> ValidatedProviderResult:
        self.calls += 1
        return GovernedOpenAIClient(
            model="gpt-4.1-mini", timeout_seconds=60, transport=self._transport
        ).understand(request)


class _StaleResults:
    def reserve(
        self,
        *,
        lease: ProcessingAttemptLease,
        details: ProviderResultReservationDetails,
    ) -> ProviderResultReservation:
        return ProviderResultReservation(
            reservation_id="reservation:1",
            reservation_state="reserved",
            reservation_generation=1,
            request_fingerprint=details.request_fingerprint,
            result_reference=None,
            provider_request_id=None,
            provider_response_id=None,
            can_call_provider=True,
        )

    def mark_in_progress(
        self,
        *,
        lease: ProcessingAttemptLease,
        reservation: ProviderResultReservation,
    ) -> ProviderResultReservation:
        return reservation

    def persist(
        self,
        *,
        lease: ProcessingAttemptLease,
        details: ProviderResultReservationDetails,
        reservation: ProviderResultReservation,
        result: ValidatedProviderResult,
    ) -> None:
        assert result.processing_attempt_id == str(lease.processing_attempt_id)
        return None
