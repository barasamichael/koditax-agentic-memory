"""Deterministic provider-processing partitions for inspected document sources."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid5
from uuid import NAMESPACE_URL
from datetime import UTC
from datetime import datetime
from dataclasses import dataclass
from collections.abc import Sequence

from shared.determinism.input_hash import compute_canonical_hash
from services.document_ai.app.governed_openai import MAX_OPENAI_SOURCE_BYTES
from services.document_ai.app.structural_scopes import StructuralScopeRecord

PROVIDER_PARTITION_POLICY_VERSION = "v1"
PROVIDER_PARTITION_NAMESPACE = uuid5(
    NAMESPACE_URL, "kodi://document-ai/provider-partition"
)
PROVIDER_PARTITION_KIND_DOCUMENT = "document"
PROVIDER_PARTITION_KIND_PAGE_RANGE = "page_range"
PROVIDER_PARTITION_KIND_SLIDE = "slide"
PROVIDER_PARTITION_KIND_WORKSHEET = "worksheet"
PROVIDER_PARTITION_KIND_LINE_RANGE = "line_range"
PROVIDER_PARTITION_KIND_ROW_RANGE = "row_range"
PROVIDER_PARTITION_KIND_PARAGRAPH_RANGE = "paragraph_range"
PROVIDER_PARTITION_KIND_IMAGE_FRAME = "image_frame"

_PARTITIONABLE_SCOPE_KINDS = frozenset(
    {
        PROVIDER_PARTITION_KIND_PAGE_RANGE,
        PROVIDER_PARTITION_KIND_SLIDE,
        PROVIDER_PARTITION_KIND_WORKSHEET,
        PROVIDER_PARTITION_KIND_LINE_RANGE,
        PROVIDER_PARTITION_KIND_ROW_RANGE,
        PROVIDER_PARTITION_KIND_PARAGRAPH_RANGE,
        PROVIDER_PARTITION_KIND_IMAGE_FRAME,
    }
)


@dataclass(frozen=True)
class ProviderPartitionRecord:
    """One deterministic, bounded provider-processing partition."""

    provider_partition_id: UUID
    tenant_id: str
    document_id: UUID
    document_version_id: UUID
    source_artifact_id: UUID
    source_inspection_id: UUID
    processing_operation_id: UUID
    policy_version: str
    partition_kind: str
    partition_ordinal: int
    parent_structural_scope_id: UUID | None
    structural_scope_ids: tuple[UUID, ...]
    structural_coordinates: dict[str, object]
    partition_payload: dict[str, object]
    partition_identity: str
    estimated_input_bytes: int
    partition_state: str
    created_at: datetime


def build_provider_partition_records(
    *,
    tenant_id: str,
    document_id: UUID,
    document_version_id: UUID,
    source_artifact_id: UUID,
    source_inspection_id: UUID,
    processing_operation_id: UUID,
    structural_scopes: Sequence[StructuralScopeRecord],
    source_size_bytes: int,
    policy_version: str = PROVIDER_PARTITION_POLICY_VERSION,
    max_partition_bytes: int = MAX_OPENAI_SOURCE_BYTES,
) -> tuple[ProviderPartitionRecord, ...]:
    """Derive stable provider partitions from deterministic structural scopes."""

    if not policy_version.strip():
        raise ValueError("provider_partition_policy_version_required")
    if source_size_bytes < 1:
        raise ValueError("provider_partition_source_size_required")
    if max_partition_bytes < 1:
        raise ValueError("provider_partition_limit_required")
    if not structural_scopes:
        return ()

    root_scope = structural_scopes[0]
    child_scopes = tuple(
        scope for scope in structural_scopes if scope.scope_kind != PROVIDER_PARTITION_KIND_DOCUMENT
    )
    if not child_scopes:
        return ()

    total_units = sum(_scope_unit_count(scope) for scope in child_scopes)
    if total_units < 1:
        total_units = len(child_scopes)
    if total_units < 1:
        return ()

    max_units_per_partition = (max_partition_bytes * total_units) // source_size_bytes
    if max_units_per_partition < 1:
        max_units_per_partition = 1

    records: list[ProviderPartitionRecord] = []
    partition_ordinal = 0
    current_kind: str | None = None
    current_unit_name: str | None = None
    current_start_unit: int | None = None
    current_end_unit: int | None = None
    current_scope_ids: list[UUID] = []
    current_scope_ordinals: list[int] = []
    current_units = 0

    def finalize_current_partition() -> None:
        nonlocal partition_ordinal
        nonlocal current_kind
        nonlocal current_unit_name
        nonlocal current_start_unit
        nonlocal current_end_unit
        nonlocal current_scope_ids
        nonlocal current_scope_ordinals
        nonlocal current_units

        if (
            current_kind is None
            or current_unit_name is None
            or current_start_unit is None
            or current_end_unit is None
            or not current_scope_ids
        ):
            return
        partition_unit_count = current_units
        estimated_input_bytes = _estimate_partition_bytes(
            source_size_bytes=source_size_bytes,
            partition_unit_count=partition_unit_count,
            total_units=total_units,
        )
        if estimated_input_bytes > max_partition_bytes:
            raise ValueError("provider_partition_limit_exceeded")
        coordinates = _build_partition_coordinates(
            scope_kind=current_kind,
            unit_name=current_unit_name,
            start=current_start_unit,
            end=current_end_unit,
        )
        payload = {
            "partition_kind": current_kind,
            "partition_ordinal": partition_ordinal,
            "source_family": root_scope.scope_payload.get("source_family"),
            "source_format": root_scope.scope_payload.get("source_format"),
            "source_size_bytes": source_size_bytes,
            "max_partition_bytes": max_partition_bytes,
            "estimated_input_bytes": estimated_input_bytes,
            "unit_name": current_unit_name,
            "unit_start": current_start_unit,
            "unit_end": current_end_unit,
            "unit_count": partition_unit_count,
            "scope_ordinal_start": min(current_scope_ordinals),
            "scope_ordinal_end": max(current_scope_ordinals),
            "structural_scope_ids": [str(scope_id) for scope_id in current_scope_ids],
        }
        records.append(
            _build_record(
                tenant_id=tenant_id,
                document_id=document_id,
                document_version_id=document_version_id,
                source_artifact_id=source_artifact_id,
                source_inspection_id=source_inspection_id,
                processing_operation_id=processing_operation_id,
                policy_version=policy_version,
                partition_kind=current_kind,
                partition_ordinal=partition_ordinal,
                parent_structural_scope_id=root_scope.structural_scope_id,
                structural_scope_ids=tuple(current_scope_ids),
                structural_coordinates=coordinates,
                partition_payload=payload,
                estimated_input_bytes=estimated_input_bytes,
                partition_state="active",
                created_at=datetime.now(UTC),
            )
        )
        partition_ordinal += 1
        current_kind = None
        current_unit_name = None
        current_start_unit = None
        current_end_unit = None
        current_scope_ids = []
        current_scope_ordinals = []
        current_units = 0

    for scope in child_scopes:
        if scope.scope_kind not in _PARTITIONABLE_SCOPE_KINDS:
            raise ValueError("provider_partition_unsupported_scope_kind")
        start_unit, end_unit, unit_name = _scope_unit_bounds(scope)
        unit_cursor = start_unit
        while unit_cursor <= end_unit:
            if current_kind is not None:
                proposed_units = current_units + 1
                proposed_estimated_bytes = _estimate_partition_bytes(
                    source_size_bytes=source_size_bytes,
                    partition_unit_count=proposed_units,
                    total_units=total_units,
                )
                if (
                    scope.scope_kind != current_kind
                    or proposed_units > max_units_per_partition
                    or proposed_estimated_bytes > max_partition_bytes
                ):
                    finalize_current_partition()
            if current_kind is None:
                current_kind = scope.scope_kind
                current_unit_name = unit_name
            if current_start_unit is None:
                current_start_unit = unit_cursor
            current_end_unit = unit_cursor
            current_units += 1
            if scope.structural_scope_id not in current_scope_ids:
                current_scope_ids.append(scope.structural_scope_id)
            current_scope_ordinals.append(scope.scope_ordinal)
            if (
                current_units == 1
                and _estimate_partition_bytes(
                    source_size_bytes=source_size_bytes,
                    partition_unit_count=1,
                    total_units=total_units,
                )
                > max_partition_bytes
            ):
                raise ValueError("provider_partition_limit_exceeded")
            unit_cursor += 1
        # Leave the current partition open so the next compatible scope can be
        # folded into the same bounded provider partition.
    finalize_current_partition()
    return tuple(records)


def load_provider_partition_records(
    *,
    cursor: object,
    tenant_id: str,
    source_inspection_id: UUID,
    policy_version: str = PROVIDER_PARTITION_POLICY_VERSION,
) -> tuple[ProviderPartitionRecord, ...]:
    """Reload persisted provider partitions in deterministic order."""

    cursor.execute(
        """
        SELECT provider_partition_id, tenant_id, document_id, document_version_id,
               source_artifact_id, source_inspection_id, processing_operation_id,
               policy_version, partition_kind, partition_ordinal, parent_structural_scope_id,
               structural_scope_ids, structural_coordinates, partition_payload,
               partition_identity, estimated_input_bytes, partition_state, created_at
        FROM document_ai_provider_partitions
        WHERE tenant_id = %s
          AND source_inspection_id = %s
          AND policy_version = %s
        ORDER BY partition_ordinal ASC
        """,
        (tenant_id, source_inspection_id, policy_version),
    )
    rows = cursor.fetchall()
    return tuple(_row_to_record(row) for row in rows)


def persist_provider_partition_records(
    *,
    cursor: object,
    tenant_id: str,
    document_id: UUID,
    document_version_id: UUID,
    source_artifact_id: UUID,
    source_inspection_id: UUID,
    processing_operation_id: UUID,
    structural_scopes: Sequence[StructuralScopeRecord],
    source_size_bytes: int,
    policy_version: str = PROVIDER_PARTITION_POLICY_VERSION,
    max_partition_bytes: int = MAX_OPENAI_SOURCE_BYTES,
) -> tuple[ProviderPartitionRecord, ...]:
    """Insert provider partitions idempotently and return the durable authority rows."""

    existing = load_provider_partition_records(
        cursor=cursor,
        tenant_id=tenant_id,
        source_inspection_id=source_inspection_id,
        policy_version=policy_version,
    )
    if existing:
        return existing

    records = build_provider_partition_records(
        tenant_id=tenant_id,
        document_id=document_id,
        document_version_id=document_version_id,
        source_artifact_id=source_artifact_id,
        source_inspection_id=source_inspection_id,
        processing_operation_id=processing_operation_id,
        structural_scopes=structural_scopes,
        source_size_bytes=source_size_bytes,
        policy_version=policy_version,
        max_partition_bytes=max_partition_bytes,
    )
    if not records:
        return ()

    for record in records:
        cursor.execute(
            """
            INSERT INTO document_ai_provider_partitions (
                provider_partition_id, tenant_id, document_id, document_version_id,
                source_artifact_id, source_inspection_id, processing_operation_id,
                policy_version, partition_kind, partition_ordinal, parent_structural_scope_id,
                structural_scope_ids, structural_coordinates, partition_payload,
                partition_identity, estimated_input_bytes, partition_state, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                      %s::jsonb, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, provider_partition_id) DO NOTHING
            """,
            (
                record.provider_partition_id,
                record.tenant_id,
                record.document_id,
                record.document_version_id,
                record.source_artifact_id,
                record.source_inspection_id,
                record.processing_operation_id,
                record.policy_version,
                record.partition_kind,
                record.partition_ordinal,
                record.parent_structural_scope_id,
                _canonical_json([str(scope_id) for scope_id in record.structural_scope_ids]),
                _canonical_json(record.structural_coordinates),
                _canonical_json(record.partition_payload),
                record.partition_identity,
                record.estimated_input_bytes,
                record.partition_state,
                record.created_at,
            ),
        )

    return load_provider_partition_records(
        cursor=cursor,
        tenant_id=tenant_id,
        source_inspection_id=source_inspection_id,
        policy_version=policy_version,
    )


def _build_partition_coordinates(
    *,
    scope_kind: str,
    unit_name: str,
    start: int,
    end: int,
) -> dict[str, object]:
    return {
        "kind": scope_kind,
        f"start_{unit_name}": start,
        f"end_{unit_name}": end,
    }


def _build_record(
    *,
    tenant_id: str,
    document_id: UUID,
    document_version_id: UUID,
    source_artifact_id: UUID,
    source_inspection_id: UUID,
    processing_operation_id: UUID,
    policy_version: str,
    partition_kind: str,
    partition_ordinal: int,
    parent_structural_scope_id: UUID | None,
    structural_scope_ids: tuple[UUID, ...],
    structural_coordinates: dict[str, object],
    partition_payload: dict[str, object],
    estimated_input_bytes: int,
    partition_state: str,
    created_at: datetime,
) -> ProviderPartitionRecord:
    identity_payload = {
        "tenant_id": tenant_id,
        "document_id": str(document_id),
        "document_version_id": str(document_version_id),
        "source_artifact_id": str(source_artifact_id),
        "source_inspection_id": str(source_inspection_id),
        "processing_operation_id": str(processing_operation_id),
        "policy_version": policy_version,
        "partition_kind": partition_kind,
        "partition_ordinal": partition_ordinal,
        "parent_structural_scope_id": (
            str(parent_structural_scope_id) if parent_structural_scope_id is not None else None
        ),
        "structural_scope_ids": [str(scope_id) for scope_id in structural_scope_ids],
        "structural_coordinates": structural_coordinates,
        "partition_payload": partition_payload,
        "estimated_input_bytes": estimated_input_bytes,
        "partition_state": partition_state,
    }
    partition_identity = compute_canonical_hash(identity_payload).sha256_hex
    return ProviderPartitionRecord(
        provider_partition_id=uuid5(PROVIDER_PARTITION_NAMESPACE, partition_identity),
        tenant_id=tenant_id,
        document_id=document_id,
        document_version_id=document_version_id,
        source_artifact_id=source_artifact_id,
        source_inspection_id=source_inspection_id,
        processing_operation_id=processing_operation_id,
        policy_version=policy_version,
        partition_kind=partition_kind,
        partition_ordinal=partition_ordinal,
        parent_structural_scope_id=parent_structural_scope_id,
        structural_scope_ids=structural_scope_ids,
        structural_coordinates=structural_coordinates,
        partition_payload=partition_payload,
        partition_identity=partition_identity,
        estimated_input_bytes=estimated_input_bytes,
        partition_state=partition_state,
        created_at=created_at,
    )


def _row_to_record(row: Sequence[object]) -> ProviderPartitionRecord:
    return ProviderPartitionRecord(
        provider_partition_id=UUID(str(row[0])),
        tenant_id=str(row[1]),
        document_id=UUID(str(row[2])),
        document_version_id=UUID(str(row[3])),
        source_artifact_id=UUID(str(row[4])),
        source_inspection_id=UUID(str(row[5])),
        processing_operation_id=UUID(str(row[6])),
        policy_version=str(row[7]),
        partition_kind=str(row[8]),
        partition_ordinal=int(row[9]),
        parent_structural_scope_id=UUID(str(row[10])) if row[10] is not None else None,
        structural_scope_ids=tuple(UUID(str(value)) for value in row[11]),
        structural_coordinates=dict(row[12]),
        partition_payload=dict(row[13]),
        partition_identity=str(row[14]),
        estimated_input_bytes=int(row[15]),
        partition_state=str(row[16]),
        created_at=row[17],
    )


def _canonical_json(value: object) -> str:
    return compute_canonical_hash(value).canonical_json


def _estimate_partition_bytes(
    *,
    source_size_bytes: int,
    partition_unit_count: int,
    total_units: int,
) -> int:
    numerator = source_size_bytes * partition_unit_count
    return max((numerator + total_units - 1) // total_units, 1)


def _scope_unit_bounds(scope: StructuralScopeRecord) -> tuple[int, int, str]:
    coordinates = scope.structural_coordinates
    kind = scope.scope_kind
    if kind == PROVIDER_PARTITION_KIND_PAGE_RANGE:
        return (
            int(coordinates.get("start_page", 1)),
            int(coordinates.get("end_page", coordinates.get("start_page", 1))),
            "page",
        )
    if kind == PROVIDER_PARTITION_KIND_LINE_RANGE:
        return (
            int(coordinates.get("start_line", 1)),
            int(coordinates.get("end_line", coordinates.get("start_line", 1))),
            "line",
        )
    if kind == PROVIDER_PARTITION_KIND_ROW_RANGE:
        return (
            int(coordinates.get("start_row", 1)),
            int(coordinates.get("end_row", coordinates.get("start_row", 1))),
            "row",
        )
    if kind == PROVIDER_PARTITION_KIND_PARAGRAPH_RANGE:
        return (
            int(coordinates.get("start_paragraph", 1)),
            int(coordinates.get("end_paragraph", coordinates.get("start_paragraph", 1))),
            "paragraph",
        )
    if kind == PROVIDER_PARTITION_KIND_SLIDE:
        slide = int(coordinates.get("slide", coordinates.get("start_slide", 1)))
        return slide, slide, "slide"
    if kind == PROVIDER_PARTITION_KIND_WORKSHEET:
        sheet = int(coordinates.get("sheet_index", 1))
        return sheet, sheet, "sheet"
    if kind == PROVIDER_PARTITION_KIND_IMAGE_FRAME:
        frame = int(coordinates.get("frame", 1))
        return frame, frame, "frame"
    raise ValueError("provider_partition_unsupported_scope_kind")


def _scope_unit_count(scope: StructuralScopeRecord) -> int:
    start, end, _unit_name = _scope_unit_bounds(scope)
    return max(end - start + 1, 1)
