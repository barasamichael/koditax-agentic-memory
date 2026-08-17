"""Deterministic provider-partition derivation coverage for Document AI."""

from __future__ import annotations

from io import BytesIO
from uuid import uuid4

from pypdf import PdfWriter

from services.document_ai.app.source_inspection import SourceInspectionResult
from services.document_ai.app.structural_scopes import build_structural_scope_records
from services.document_ai.app.provider_partitions import build_provider_partition_records
from services.document_ai.app.provider_partitions import PROVIDER_PARTITION_KIND_LINE_RANGE
from services.document_ai.app.provider_partitions import PROVIDER_PARTITION_KIND_PAGE_RANGE


def test_pdf_sources_are_partitioned_into_bounded_provider_ranges() -> None:
    payload = _build_pdf_payload(page_count=52)
    inspection = _accepted_inspection(
        media_type="application/pdf",
        source_family="pdf",
        source_format="pdf",
        size_bytes=20 * 1024 * 1024,
    )
    scopes = build_structural_scope_records(
        tenant_id="tenant-a",
        document_id=uuid4(),
        document_version_id=uuid4(),
        source_artifact_id=uuid4(),
        source_inspection_id=uuid4(),
        processing_operation_id=uuid4(),
        inspection=inspection,
        source_payload=payload,
    )

    records = build_provider_partition_records(
        tenant_id="tenant-a",
        document_id=scopes[0].document_id,
        document_version_id=scopes[0].document_version_id,
        source_artifact_id=scopes[0].source_artifact_id,
        source_inspection_id=scopes[0].source_inspection_id,
        processing_operation_id=scopes[0].processing_operation_id,
        structural_scopes=scopes,
        source_size_bytes=inspection.source_size_bytes,
    )
    replay = build_provider_partition_records(
        tenant_id="tenant-a",
        document_id=scopes[0].document_id,
        document_version_id=scopes[0].document_version_id,
        source_artifact_id=scopes[0].source_artifact_id,
        source_inspection_id=scopes[0].source_inspection_id,
        processing_operation_id=scopes[0].processing_operation_id,
        structural_scopes=scopes,
        source_size_bytes=inspection.source_size_bytes,
    )

    assert len(records) == 3
    assert [record.partition_ordinal for record in records] == [0, 1, 2]
    assert records[0].partition_kind == PROVIDER_PARTITION_KIND_PAGE_RANGE
    assert records[0].structural_coordinates == {
        "kind": PROVIDER_PARTITION_KIND_PAGE_RANGE,
        "start_page": 1,
        "end_page": 20,
    }
    assert records[1].structural_coordinates == {
        "kind": PROVIDER_PARTITION_KIND_PAGE_RANGE,
        "start_page": 21,
        "end_page": 40,
    }
    assert records[2].structural_coordinates == {
        "kind": PROVIDER_PARTITION_KIND_PAGE_RANGE,
        "start_page": 41,
        "end_page": 52,
    }
    assert records[0].partition_payload["estimated_input_bytes"] <= 8 * 1024 * 1024
    assert [record.provider_partition_id for record in records] == [
        record.provider_partition_id for record in replay
    ]
    assert [record.partition_identity for record in records] == [
        record.partition_identity for record in replay
    ]


def test_text_sources_are_partitioned_into_stable_provider_line_ranges() -> None:
    payload = "\n".join(f"line {index}" for index in range(1, 121)).encode("utf-8")
    inspection = _accepted_inspection(
        media_type="text/plain",
        source_family="text",
        source_format="plain",
        size_bytes=40 * 1024 * 1024,
    )
    scopes = build_structural_scope_records(
        tenant_id="tenant-a",
        document_id=uuid4(),
        document_version_id=uuid4(),
        source_artifact_id=uuid4(),
        source_inspection_id=uuid4(),
        processing_operation_id=uuid4(),
        inspection=inspection,
        source_payload=payload,
    )

    records = build_provider_partition_records(
        tenant_id="tenant-a",
        document_id=scopes[0].document_id,
        document_version_id=scopes[0].document_version_id,
        source_artifact_id=scopes[0].source_artifact_id,
        source_inspection_id=scopes[0].source_inspection_id,
        processing_operation_id=scopes[0].processing_operation_id,
        structural_scopes=scopes,
        source_size_bytes=inspection.source_size_bytes,
    )

    assert len(records) == 5
    assert records[0].partition_kind == PROVIDER_PARTITION_KIND_LINE_RANGE
    assert records[0].structural_coordinates == {
        "kind": PROVIDER_PARTITION_KIND_LINE_RANGE,
        "start_line": 1,
        "end_line": 24,
    }
    assert records[-1].structural_coordinates == {
        "kind": PROVIDER_PARTITION_KIND_LINE_RANGE,
        "start_line": 97,
        "end_line": 120,
    }
    assert sum(record.partition_payload["unit_count"] for record in records) == 120


def _accepted_inspection(
    *,
    media_type: str,
    source_family: str,
    source_format: str,
    size_bytes: int,
) -> SourceInspectionResult:
    return SourceInspectionResult(
        policy_version="v1",
        disposition="accepted",
        reason="accepted",
        observed_media_type=media_type,
        observed_source_family=source_family,
        observed_source_format=source_format,
        declared_media_type=media_type,
        source_size_bytes=size_bytes,
        page_count=None,
        structural_scopes=(),
        diagnostic_payload={},
    )


def _build_pdf_payload(*, page_count: int) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()
