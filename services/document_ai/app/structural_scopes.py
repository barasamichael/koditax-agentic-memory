"""Deterministic structural scope derivation and CockroachDB persistence."""

from __future__ import annotations

from io import BytesIO
from io import TextIOWrapper
import re
from uuid import UUID
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
from typing import Protocol
import zipfile
from datetime import UTC
from datetime import datetime
from xml.etree import ElementTree
from dataclasses import dataclass
from collections.abc import Sequence

from pypdf import PdfReader

from shared.determinism.input_hash import compute_canonical_hash
from services.document_ai.app.config import SOURCE_INSPECTION_SCOPE_SIZE
from services.document_ai.app.document_formats import normalize_media_type
from services.document_ai.app.source_inspection import SourceInspectionResult

STRUCTURAL_SCOPE_POLICY_VERSION = "v1"
STRUCTURAL_SCOPE_NAMESPACE = uuid5(NAMESPACE_URL, "kodi://document-ai/structural-scope")
STRUCTURAL_SCOPE_KIND_ROOT = "document"
STRUCTURAL_SCOPE_KIND_PAGE_RANGE = "page_range"
STRUCTURAL_SCOPE_KIND_SLIDE = "slide"
STRUCTURAL_SCOPE_KIND_WORKSHEET = "worksheet"
STRUCTURAL_SCOPE_KIND_LINE_RANGE = "line_range"
STRUCTURAL_SCOPE_KIND_ROW_RANGE = "row_range"
STRUCTURAL_SCOPE_KIND_PARAGRAPH_RANGE = "paragraph_range"
STRUCTURAL_SCOPE_KIND_IMAGE_FRAME = "image_frame"

_RTF_PARAGRAPH_PATTERN = re.compile(rb"\\par[ }\\]")


class _Cursor(Protocol):
    def execute(self, query: str, params: tuple[object, ...]) -> object:
        """Execute one parameterized command."""

    def fetchone(self) -> Sequence[object] | None:
        """Return one row from the last query."""

    def fetchall(self) -> list[Sequence[object]]:
        """Return all rows from the last query."""


@dataclass(frozen=True)
class StructuralScopeRecord:
    """One deterministic, source-aware processing unit."""

    structural_scope_id: UUID
    tenant_id: str
    document_id: UUID
    document_version_id: UUID
    source_artifact_id: UUID
    source_inspection_id: UUID
    processing_operation_id: UUID
    policy_version: str
    scope_kind: str
    scope_ordinal: int
    parent_structural_scope_id: UUID | None
    structural_coordinates: dict[str, object]
    scope_payload: dict[str, object]
    scope_identity: str
    created_at: datetime


def build_structural_scope_records(
    *,
    tenant_id: str,
    document_id: UUID,
    document_version_id: UUID,
    source_artifact_id: UUID,
    source_inspection_id: UUID,
    processing_operation_id: UUID,
    inspection: SourceInspectionResult,
    source_payload: bytes,
    policy_version: str = STRUCTURAL_SCOPE_POLICY_VERSION,
) -> tuple[StructuralScopeRecord, ...]:
    """Derive stable structural scopes from one accepted inspected source."""

    if inspection.disposition != "accepted":
        return ()
    source_family = inspection.observed_source_family
    source_format = inspection.observed_source_format
    root_coordinates = {
        "kind": STRUCTURAL_SCOPE_KIND_ROOT,
        "start": 1,
        "end": 1,
    }
    root_payload = {
        "scope_role": "root",
        "source_family": source_family,
        "source_format": source_format,
        "source_size_bytes": inspection.source_size_bytes,
    }
    root = _build_record(
        tenant_id=tenant_id,
        document_id=document_id,
        document_version_id=document_version_id,
        source_artifact_id=source_artifact_id,
        source_inspection_id=source_inspection_id,
        processing_operation_id=processing_operation_id,
        policy_version=policy_version,
        scope_kind=STRUCTURAL_SCOPE_KIND_ROOT,
        scope_ordinal=0,
        parent_structural_scope_id=None,
        structural_coordinates=root_coordinates,
        scope_payload=root_payload,
        created_at=datetime.now(UTC),
    )

    child_scopes = _build_child_scope_specs(
        source_family=source_family,
        source_format=source_format,
        source_payload=source_payload,
    )
    records = [root]
    for ordinal, (scope_kind, coordinates, payload) in enumerate(child_scopes, start=1):
        records.append(
            _build_record(
                tenant_id=tenant_id,
                document_id=document_id,
                document_version_id=document_version_id,
                source_artifact_id=source_artifact_id,
                source_inspection_id=source_inspection_id,
                processing_operation_id=processing_operation_id,
                policy_version=policy_version,
                scope_kind=scope_kind,
                scope_ordinal=ordinal,
                parent_structural_scope_id=root.structural_scope_id,
                structural_coordinates=coordinates,
                scope_payload=payload,
                created_at=datetime.now(UTC),
            )
        )
    return tuple(records)


def load_structural_scope_records(
    *,
    cursor: _Cursor,
    tenant_id: str,
    source_inspection_id: UUID,
    policy_version: str = STRUCTURAL_SCOPE_POLICY_VERSION,
) -> tuple[StructuralScopeRecord, ...]:
    """Reload previously persisted structural scopes in deterministic order."""

    cursor.execute(
        """
        SELECT structural_scope_id, tenant_id, document_id, document_version_id,
               source_artifact_id, source_inspection_id, processing_operation_id,
               policy_version, scope_kind, scope_ordinal, parent_structural_scope_id,
               structural_coordinates, scope_payload, scope_identity, created_at
        FROM document_ai_structural_scopes
        WHERE tenant_id = %s
          AND source_inspection_id = %s
          AND policy_version = %s
        ORDER BY scope_ordinal ASC
        """,
        (tenant_id, source_inspection_id, policy_version),
    )
    rows = cursor.fetchall()
    return tuple(_row_to_record(row) for row in rows)


def persist_structural_scope_records(
    *,
    cursor: _Cursor,
    tenant_id: str,
    document_id: UUID,
    document_version_id: UUID,
    source_artifact_id: UUID,
    source_inspection_id: UUID,
    processing_operation_id: UUID,
    inspection: SourceInspectionResult,
    source_payload: bytes,
    policy_version: str = STRUCTURAL_SCOPE_POLICY_VERSION,
) -> tuple[StructuralScopeRecord, ...]:
    """Insert structural scopes idempotently and return the durable authority rows."""

    existing = load_structural_scope_records(
        cursor=cursor,
        tenant_id=tenant_id,
        source_inspection_id=source_inspection_id,
        policy_version=policy_version,
    )
    if existing:
        return existing
    records = build_structural_scope_records(
        tenant_id=tenant_id,
        document_id=document_id,
        document_version_id=document_version_id,
        source_artifact_id=source_artifact_id,
        source_inspection_id=source_inspection_id,
        processing_operation_id=processing_operation_id,
        inspection=inspection,
        source_payload=source_payload,
        policy_version=policy_version,
    )
    if not records:
        return ()
    for record in records:
        cursor.execute(
            """
            INSERT INTO document_ai_structural_scopes (
                structural_scope_id, tenant_id, document_id, document_version_id,
                source_artifact_id, source_inspection_id, processing_operation_id,
                policy_version, scope_kind, scope_ordinal, parent_structural_scope_id,
                structural_coordinates, scope_payload, scope_identity, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
            ON CONFLICT (tenant_id, structural_scope_id) DO NOTHING
            """,
            (
                record.structural_scope_id,
                record.tenant_id,
                record.document_id,
                record.document_version_id,
                record.source_artifact_id,
                record.source_inspection_id,
                record.processing_operation_id,
                record.policy_version,
                record.scope_kind,
                record.scope_ordinal,
                record.parent_structural_scope_id,
                compute_canonical_json(record.structural_coordinates),
                compute_canonical_json(record.scope_payload),
                record.scope_identity,
                record.created_at,
            ),
        )
    return load_structural_scope_records(
        cursor=cursor,
        tenant_id=tenant_id,
        source_inspection_id=source_inspection_id,
        policy_version=policy_version,
    )


def compute_canonical_json(value: dict[str, object]) -> str:
    """Serialize one structural payload deterministically."""

    return compute_canonical_hash(value).canonical_json


def _build_child_scope_specs(
    *,
    source_family: str | None,
    source_format: str | None,
    source_payload: bytes,
) -> tuple[tuple[str, dict[str, object], dict[str, object]], ...]:
    family = source_family or "text"
    if family == "pdf":
        return _build_pdf_scope_specs(source_payload=source_payload, source_format=source_format)
    if family == "presentation":
        return _build_presentation_scope_specs(
            source_payload=source_payload, source_format=source_format
        )
    if family == "spreadsheet":
        return _build_spreadsheet_scope_specs(
            source_payload=source_payload, source_format=source_format
        )
    if family == "word_processing":
        return _build_word_processing_scope_specs(
            source_payload=source_payload, source_format=source_format
        )
    if family == "image":
        return (
            (
                STRUCTURAL_SCOPE_KIND_IMAGE_FRAME,
                {"kind": STRUCTURAL_SCOPE_KIND_IMAGE_FRAME, "frame": 1},
                _scope_payload(
                    source_family=family,
                    source_format=source_format,
                    scope_kind=STRUCTURAL_SCOPE_KIND_IMAGE_FRAME,
                    scope_label="frame-1",
                    start=1,
                    end=1,
                ),
            ),
        )
    if family == "text":
        if source_format == "csv" or source_format == "tsv":
            return _build_line_scopes(
                source_payload=source_payload,
                source_format=source_format,
                scope_kind=STRUCTURAL_SCOPE_KIND_ROW_RANGE,
                coordinate_prefix="row",
                source_family="text",
            )
        return _build_line_scopes(
            source_payload=source_payload,
            source_format=source_format,
            scope_kind=STRUCTURAL_SCOPE_KIND_LINE_RANGE,
            coordinate_prefix="line",
            source_family="text",
        )
    return _build_line_scopes(
        source_payload=source_payload,
        source_format=source_format,
        scope_kind=STRUCTURAL_SCOPE_KIND_LINE_RANGE,
        coordinate_prefix="line",
        source_family=family,
    )


def _build_pdf_scope_specs(
    *,
    source_payload: bytes,
    source_format: str | None,
) -> tuple[tuple[str, dict[str, object], dict[str, object]], ...]:
    try:
        page_count = len(PdfReader(BytesIO(source_payload)).pages)
    except Exception:
        page_count = 1
    return _build_range_scopes(
        unit_name="page",
        scope_kind=STRUCTURAL_SCOPE_KIND_PAGE_RANGE,
        total_units=page_count,
        source_format=source_format,
        source_family="pdf",
    )


def _build_presentation_scope_specs(
    *,
    source_payload: bytes,
    source_format: str | None,
) -> tuple[tuple[str, dict[str, object], dict[str, object]], ...]:
    slide_count = _count_presentation_slides(source_payload)
    return _build_range_scopes(
        unit_name="slide",
        scope_kind=STRUCTURAL_SCOPE_KIND_SLIDE,
        total_units=slide_count,
        source_format=source_format,
        source_family="presentation",
    )


def _build_spreadsheet_scope_specs(
    *,
    source_payload: bytes,
    source_format: str | None,
) -> tuple[tuple[str, dict[str, object], dict[str, object]], ...]:
    sheets = _extract_spreadsheet_sheet_names(source_payload)
    if not sheets:
        sheets = ("Sheet1",)
    specs: list[tuple[str, dict[str, object], dict[str, object]]] = []
    for index, sheet_name in enumerate(sheets, start=1):
        coordinates = {
            "kind": STRUCTURAL_SCOPE_KIND_WORKSHEET,
            "sheet_index": index,
            "sheet_name": sheet_name,
        }
        payload = _scope_payload(
            source_family="spreadsheet",
            source_format=source_format,
            scope_kind=STRUCTURAL_SCOPE_KIND_WORKSHEET,
            scope_label=sheet_name,
            start=index,
            end=index,
        )
        specs.append((STRUCTURAL_SCOPE_KIND_WORKSHEET, coordinates, payload))
    return tuple(specs)


def _build_word_processing_scope_specs(
    *,
    source_payload: bytes,
    source_format: str | None,
) -> tuple[tuple[str, dict[str, object], dict[str, object]], ...]:
    total_units = _count_word_processing_blocks(source_payload, source_format)
    return _build_range_scopes(
        unit_name="paragraph",
        scope_kind=STRUCTURAL_SCOPE_KIND_PARAGRAPH_RANGE,
        total_units=total_units,
        source_format=source_format,
        source_family="word_processing",
    )


def _build_line_scopes(
    *,
    source_payload: bytes,
    source_format: str | None,
    scope_kind: str,
    coordinate_prefix: str,
    source_family: str,
) -> tuple[tuple[str, dict[str, object], dict[str, object]], ...]:
    total_units = _count_text_units(source_payload)
    return _build_range_scopes(
        unit_name=coordinate_prefix,
        scope_kind=scope_kind,
        total_units=total_units,
        source_format=source_format,
        source_family=source_family,
    )


def _build_range_scopes(
    *,
    unit_name: str,
    scope_kind: str,
    total_units: int,
    source_format: str | None,
    source_family: str,
) -> tuple[tuple[str, dict[str, object], dict[str, object]], ...]:
    if total_units < 1:
        total_units = 1
    specs: list[tuple[str, dict[str, object], dict[str, object]]] = []
    for start in range(1, total_units + 1, SOURCE_INSPECTION_SCOPE_SIZE):
        end = min(start + SOURCE_INSPECTION_SCOPE_SIZE - 1, total_units)
        coordinates = {
            "kind": scope_kind,
            f"start_{unit_name}": start,
            f"end_{unit_name}": end,
        }
        payload = _scope_payload(
            source_family=source_family,
            source_format=source_format,
            scope_kind=scope_kind,
            scope_label=f"{unit_name}-{start}-{end}",
            start=start,
            end=end,
        )
        specs.append((scope_kind, coordinates, payload))
    return tuple(specs)


def _count_text_units(source_payload: bytes) -> int:
    count = 0
    with TextIOWrapper(BytesIO(source_payload), encoding="utf-8", newline="") as stream:
        for _line in stream:
            count += 1
    return max(count, 1)


def _count_word_processing_blocks(source_payload: bytes, source_format: str | None) -> int:
    if source_format == "rtf":
        return max(len(_RTF_PARAGRAPH_PATTERN.findall(source_payload)) + 1, 1)
    try:
        with zipfile.ZipFile(BytesIO(source_payload)) as archive:
            if "word/document.xml" in archive.namelist():
                return max(_count_xml_blocks(archive.read("word/document.xml")), 1)
            if "content.xml" in archive.namelist():
                return max(_count_xml_blocks(archive.read("content.xml")), 1)
    except zipfile.BadZipFile:
        return 1
    return 1


def _count_presentation_slides(source_payload: bytes) -> int:
    try:
        with zipfile.ZipFile(BytesIO(source_payload)) as archive:
            presentation = archive.read("ppt/presentation.xml")
    except (KeyError, zipfile.BadZipFile):
        return 1
    root = ElementTree.fromstring(presentation)
    return max(
        sum(1 for element in root.iter() if element.tag.endswith("}sldId")),
        1,
    )


def _extract_spreadsheet_sheet_names(source_payload: bytes) -> tuple[str, ...]:
    try:
        with zipfile.ZipFile(BytesIO(source_payload)) as archive:
            workbook = archive.read("xl/workbook.xml")
    except (KeyError, zipfile.BadZipFile):
        return ()
    root = ElementTree.fromstring(workbook)
    sheet_names: list[str] = []
    for element in root.iter():
        if not element.tag.endswith("}sheet"):
            continue
        name = element.attrib.get("name")
        if isinstance(name, str) and name:
            sheet_names.append(name)
    return tuple(sheet_names)


def _count_xml_blocks(xml_payload: bytes) -> int:
    root = ElementTree.fromstring(xml_payload)
    count = 0
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name in {"p", "tbl", "table"}:
            count += 1
    return count


def _build_record(
    *,
    tenant_id: str,
    document_id: UUID,
    document_version_id: UUID,
    source_artifact_id: UUID,
    source_inspection_id: UUID,
    processing_operation_id: UUID,
    policy_version: str,
    scope_kind: str,
    scope_ordinal: int,
    parent_structural_scope_id: UUID | None,
    structural_coordinates: dict[str, object],
    scope_payload: dict[str, object],
    created_at: datetime,
) -> StructuralScopeRecord:
    identity_payload = {
        "tenant_id": tenant_id,
        "document_id": str(document_id),
        "document_version_id": str(document_version_id),
        "source_artifact_id": str(source_artifact_id),
        "source_inspection_id": str(source_inspection_id),
        "processing_operation_id": str(processing_operation_id),
        "policy_version": policy_version,
        "scope_kind": scope_kind,
        "scope_ordinal": scope_ordinal,
        "parent_structural_scope_id": str(parent_structural_scope_id)
        if parent_structural_scope_id is not None
        else None,
        "structural_coordinates": structural_coordinates,
        "scope_payload": scope_payload,
    }
    scope_identity = compute_canonical_hash(identity_payload).sha256_hex
    return StructuralScopeRecord(
        structural_scope_id=uuid5(STRUCTURAL_SCOPE_NAMESPACE, scope_identity),
        tenant_id=tenant_id,
        document_id=document_id,
        document_version_id=document_version_id,
        source_artifact_id=source_artifact_id,
        source_inspection_id=source_inspection_id,
        processing_operation_id=processing_operation_id,
        policy_version=policy_version,
        scope_kind=scope_kind,
        scope_ordinal=scope_ordinal,
        parent_structural_scope_id=parent_structural_scope_id,
        structural_coordinates=structural_coordinates,
        scope_payload=scope_payload,
        scope_identity=scope_identity,
        created_at=created_at,
    )


def _row_to_record(row: Sequence[object]) -> StructuralScopeRecord:
    return StructuralScopeRecord(
        structural_scope_id=UUID(str(row[0])),
        tenant_id=str(row[1]),
        document_id=UUID(str(row[2])),
        document_version_id=UUID(str(row[3])),
        source_artifact_id=UUID(str(row[4])),
        source_inspection_id=UUID(str(row[5])),
        processing_operation_id=UUID(str(row[6])),
        policy_version=str(row[7]),
        scope_kind=str(row[8]),
        scope_ordinal=int(row[9]),
        parent_structural_scope_id=UUID(str(row[10])) if row[10] is not None else None,
        structural_coordinates=dict(cast(dict[str, object], row[11])),
        scope_payload=dict(cast(dict[str, object], row[12])),
        scope_identity=str(row[13]),
        created_at=row[14],
    )


def _scope_payload(
    *,
    source_family: str | None,
    source_format: str | None,
    scope_kind: str,
    scope_label: str,
    start: int,
    end: int,
) -> dict[str, object]:
    return {
        "scope_kind": scope_kind,
        "scope_label": scope_label,
        "source_family": source_family,
        "source_format": normalize_media_type(source_format or source_family or ""),
        "start": start,
        "end": end,
        "scope_size": end - start + 1,
        "window_size": SOURCE_INSPECTION_SCOPE_SIZE,
    }
