"""Validation and readiness decisions for inactive canonical candidates."""

from __future__ import annotations

import json
from uuid import UUID
from typing import cast
from typing import Literal
from typing import Protocol
from dataclasses import field
from dataclasses import dataclass
from collections.abc import Mapping
from collections.abc import Sequence

from shared.determinism.input_hash import compute_canonical_hash
from services.document_ai.app.canonical_assembly import CanonicalGraph
from services.document_ai.app.canonical_assembly import CanonicalElement
from services.document_ai.app.canonical_assembly import CANONICAL_SCHEMA_VERSION
from services.document_ai.app.canonical_assembly import CANONICAL_ASSEMBLY_POLICY_VERSION
from services.document_ai.app.processing_workers import ProcessingAttemptLease
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction

CanonicalCandidateState = Literal["validated", "rejected"]
CanonicalReadiness = Literal["full", "none"]
CANONICAL_VALIDATION_VERSION = "v1"
CANONICAL_VALIDATION_CONTINUATION_EVENT = "canonical_chunking_requested"


class _Cursor(Protocol):
    def execute(self, query: str, params: tuple[object, ...]) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> list[Sequence[object]]: ...


@dataclass(frozen=True)
class CanonicalCandidate:
    """An immutable, source-bound candidate that has not yet become active."""

    canonical_representation_id: UUID
    provider_result_id: UUID
    source_artifact_id: UUID
    document_version_id: UUID
    canonical_schema_version: str
    assembly_policy_version: str
    graph: CanonicalGraph


@dataclass(frozen=True)
class CanonicalValidationResult:
    """A durable validation decision and its persisted report."""

    state: CanonicalCandidateState
    readiness: CanonicalReadiness
    missing_pages: tuple[int, ...]
    reasons: tuple[str, ...]
    validation_report: dict[str, object] = field(default_factory=dict)


class CanonicalValidationError(RuntimeError):
    """Represent a validation authority failure."""

    def __init__(self, reason: str, *, retryable: bool, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.retryable = retryable
        self.message = message


def validate_canonical_candidate(
    *,
    candidate: CanonicalCandidate,
    expected_source_artifact_id: UUID,
    expected_pages: tuple[int, ...],
    expected_document_version_id: UUID | None = None,
    expected_provider_result_id: UUID | None = None,
    expected_processing_operation_id: UUID | None = None,
    expected_page_count: int | None = None,
) -> CanonicalValidationResult:
    """Validate lineage, ordering, and complete structural coverage."""

    reasons: list[str] = []
    payload = candidate.graph.payload
    if candidate.canonical_schema_version != CANONICAL_SCHEMA_VERSION:
        reasons.append("canonical_schema_version_mismatch")
    if candidate.assembly_policy_version != CANONICAL_ASSEMBLY_POLICY_VERSION:
        reasons.append("canonical_assembly_policy_mismatch")
    if payload.get("schema_version") != candidate.canonical_schema_version:
        reasons.append("canonical_payload_schema_mismatch")
    if payload.get("assembly_policy_version") != candidate.assembly_policy_version:
        reasons.append("canonical_payload_policy_mismatch")

    source_lineage = payload.get("source_lineage")
    if not isinstance(source_lineage, Mapping):
        reasons.append("canonical_source_lineage_missing")
        source_lineage_map: Mapping[str, object] = {}
    else:
        source_lineage_map = cast(Mapping[str, object], source_lineage)
        _validate_source_lineage(
            reasons=reasons,
            lineage=source_lineage_map,
            expected_source_artifact_id=expected_source_artifact_id,
            expected_document_version_id=expected_document_version_id,
            expected_provider_result_id=expected_provider_result_id,
            expected_processing_operation_id=expected_processing_operation_id,
        )

    if candidate.source_artifact_id != expected_source_artifact_id:
        reasons.append("source_artifact_mismatch")
    if candidate.graph.source_lineage and source_lineage_map:
        if candidate.graph.source_lineage != dict(source_lineage_map):
            reasons.append("canonical_source_lineage_mismatch")

    structural_units = payload.get("structural_units")
    covered_pages: set[int] = set()
    if not isinstance(structural_units, list):
        reasons.append("canonical_structural_units_invalid")
    else:
        covered_pages = _validate_structural_units(
            structural_units=cast(list[object], structural_units), reasons=reasons
        )

    if expected_pages:
        expected_page_set = set(expected_pages)
        missing_pages = tuple(sorted(expected_page_set - covered_pages))
        extra_pages = tuple(sorted(covered_pages - expected_page_set))
        if missing_pages:
            reasons.append("canonical_structural_coverage_incomplete")
        if extra_pages:
            reasons.append("canonical_structural_coverage_mismatch")
    else:
        if covered_pages:
            reasons.append("canonical_structural_coverage_mismatch")
        missing_pages = ()

    element_count = payload.get("element_count")
    elements = tuple(candidate.graph.elements)
    if not isinstance(element_count, int) or isinstance(element_count, bool):
        reasons.append("canonical_element_count_invalid")
    elif element_count != len(elements):
        reasons.append("canonical_element_count_mismatch")

    if list(elements) != sorted(elements, key=_element_sort_key):
        reasons.append("canonical_element_order_invalid")

    stable_keys: set[str] = set()
    page_groups: dict[int, list[CanonicalElement]] = {}
    for element in elements:
        if not element.stable_key or element.stable_key in stable_keys:
            reasons.append("canonical_element_identity_invalid")
        stable_keys.add(element.stable_key)

        if not isinstance(element.page_number, int) or isinstance(element.page_number, bool):
            reasons.append("canonical_element_page_invalid")
            continue
        if element.page_number < 1:
            reasons.append("canonical_element_page_invalid")
        page_groups.setdefault(element.page_number, []).append(element)

        if not isinstance(element.reading_order, int) or isinstance(element.reading_order, bool):
            reasons.append("canonical_reading_order_invalid")
        elif element.reading_order < 0:
            reasons.append("canonical_reading_order_invalid")

        source_region = element.source_region
        if not isinstance(source_region, Mapping):
            reasons.append("missing_source_provenance")
        else:
            source_page = source_region.get("page_number")
            if (
                not isinstance(source_page, int)
                or isinstance(source_page, bool)
                or source_page != element.page_number
            ):
                reasons.append("missing_source_provenance")
            if expected_page_count is not None and element.page_number > expected_page_count:
                reasons.append("canonical_page_out_of_bounds")

        if element.page_number not in covered_pages:
            reasons.append("canonical_element_outside_structural_scope")

    _validate_page_reading_orders(page_groups=page_groups, reasons=reasons)

    if _detect_parent_cycle(elements):
        reasons.append("canonical_parent_cycle_detected")

    if candidate.graph.content_hash != _compute_graph_hash(candidate.graph):
        reasons.append("canonical_content_hash_mismatch")

    report = {
        "canonical_validation_version": CANONICAL_VALIDATION_VERSION,
        "canonical_schema_version": candidate.canonical_schema_version,
        "assembly_policy_version": candidate.assembly_policy_version,
        "expected_pages": list(expected_pages),
        "covered_pages": sorted(covered_pages),
        "missing_pages": list(missing_pages),
        "element_count": len(elements),
        "content_hash": candidate.graph.content_hash,
        "reason_codes": sorted(set(reasons)),
        "source_lineage": dict(source_lineage_map),
    }
    if reasons:
        return CanonicalValidationResult(
            state="rejected",
            readiness="none",
            missing_pages=missing_pages,
            reasons=tuple(sorted(set(reasons))),
            validation_report=report,
        )
    return CanonicalValidationResult(
        state="validated",
        readiness="full",
        missing_pages=missing_pages,
        reasons=(),
        validation_report=report,
    )


class CanonicalValidationRepository:
    """Persist a canonical validation authority result without rewriting content."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def validate_canonical_representation(
        self, *, tenant_id: str, canonical_representation_id: UUID
    ) -> CanonicalValidationResult:
        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.canonical_validation.validate_candidate",
            transaction_callback=lambda cursor: self._validate_transaction(
                cursor=cursor,
                tenant_id=tenant_id,
                canonical_representation_id=canonical_representation_id,
            ),
            reconcile_ambiguous_result=lambda connection: self._reconcile_validation_result(
                connection=connection,
                tenant_id=tenant_id,
                canonical_representation_id=canonical_representation_id,
            ),
        )

    def validate_for_lease(self, *, lease: ProcessingAttemptLease) -> CanonicalValidationResult:
        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT representation.canonical_representation_id
                      FROM document_ai_canonical_representations AS representation
                      JOIN document_ai_processing_operations AS operation
                        ON operation.tenant_id = representation.tenant_id
                       AND operation.processing_operation_id =
                           representation.processing_operation_id
                      JOIN document_ai_processing_work_items AS work
                        ON work.tenant_id = operation.tenant_id
                       AND work.processing_operation_id = operation.processing_operation_id
                      JOIN document_ai_processing_attempts AS attempt
                        ON attempt.tenant_id = work.tenant_id
                       AND attempt.processing_attempt_id = work.current_processing_attempt_id
                     WHERE representation.tenant_id = %s
                       AND operation.processing_operation_id = %s
                       AND work.processing_work_item_id = %s
                       AND work.current_processing_attempt_id = %s
                       AND work.fencing_token = %s
                       AND work.state = 'leased'
                       AND work.leased_until > now()
                       AND attempt.state = 'running'
                       AND attempt.fencing_token = %s
                       AND operation.cancellation_requested_at IS NULL
                     FOR UPDATE""",
                    (
                        lease.tenant_id,
                        lease.processing_operation_id,
                        lease.processing_work_item_id,
                        lease.processing_attempt_id,
                        lease.fencing_token,
                        lease.fencing_token,
                    ),
                )
                row = cursor.fetchone()
        if row is None:
            raise CanonicalValidationError(
                "canonical_candidate_not_available",
                retryable=True,
                message="The fenced canonical candidate is not yet available for validation.",
            )
        return self.validate_canonical_representation(
            tenant_id=lease.tenant_id, canonical_representation_id=UUID(str(row[0]))
        )

    def _validate_transaction(
        self,
        *,
        cursor: _Cursor,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> CanonicalValidationResult:
        row = self._load_candidate_row(
            cursor=cursor,
            tenant_id=tenant_id,
            canonical_representation_id=canonical_representation_id,
        )
        if row is None:
            raise CanonicalValidationError(
                "canonical_candidate_missing",
                retryable=True,
                message="The canonical candidate is not yet durably visible.",
            )

        existing = self._reconstruct_existing_result(row=row)
        if existing is not None:
            if existing.state == "validated":
                self._ensure_continuation_outbox(
                    cursor=cursor,
                    row=row,
                    validation_version=existing.validation_report.get(
                        "canonical_validation_version", CANONICAL_VALIDATION_VERSION
                    ),
                )
            return existing

        candidate = self._build_candidate(cursor=cursor, row=row)
        expected_pages = _expected_pages_from_provider_result(row["validated_result"])
        expected_scope_ids = _expected_structural_scope_ids(cursor=cursor, row=row)
        result = validate_canonical_candidate(
            candidate=candidate,
            expected_source_artifact_id=UUID(str(row["source_artifact_id"])),
            expected_pages=expected_pages,
            expected_document_version_id=UUID(str(row["document_version_id"])),
            expected_provider_result_id=UUID(str(row["provider_result_id"])),
            expected_processing_operation_id=UUID(str(row["processing_operation_id"])),
            expected_page_count=int(row["page_count"]) if row["page_count"] is not None else None,
        )
        result = self._apply_authority_checks(
            cursor=cursor,
            row=row,
            result=result,
            expected_scope_ids=expected_scope_ids,
        )
        self._persist_validation_result(
            cursor=cursor,
            tenant_id=tenant_id,
            canonical_representation_id=canonical_representation_id,
            result=result,
        )
        return result

    def _load_candidate_row(
        self,
        *,
        cursor: _Cursor,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> dict[str, object] | None:
        cursor.execute(
            """
            SELECT representation.canonical_representation_id,
                   representation.tenant_id,
                   representation.document_version_id,
                   representation.processing_operation_id,
                   representation.canonical_schema_version,
                   representation.assembly_policy_version,
                   representation.source_artifact_id,
                   representation.provider_result_id,
                   representation.representation_payload,
                   representation.state,
                   representation.readiness_state,
                   representation.canonical_validation_version,
                   representation.validation_report,
                   representation.validated_at,
                   representation.rejected_at,
                   provider.source_scope_id,
                   provider.validated_result,
                   inspection.source_inspection_id,
                   inspection.page_count
              FROM document_ai_canonical_representations AS representation
              JOIN document_ai_provider_results AS provider
                ON provider.tenant_id = representation.tenant_id
               AND provider.provider_result_id = representation.provider_result_id
              JOIN document_ai_source_inspections AS inspection
                ON inspection.tenant_id = representation.tenant_id
               AND inspection.document_version_id = representation.document_version_id
               AND inspection.source_artifact_id = representation.source_artifact_id
             WHERE representation.tenant_id = %s
               AND representation.canonical_representation_id = %s
             FOR UPDATE""",
            (tenant_id, canonical_representation_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "canonical_representation_id": row[0],
            "tenant_id": row[1],
            "document_version_id": row[2],
            "processing_operation_id": row[3],
            "canonical_schema_version": row[4],
            "assembly_policy_version": row[5],
            "source_artifact_id": row[6],
            "provider_result_id": row[7],
            "representation_payload": row[8],
            "state": row[9],
            "readiness_state": row[10],
            "canonical_validation_version": row[11],
            "validation_report": row[12],
            "validated_at": row[13],
            "rejected_at": row[14],
            "source_scope_id": row[15],
            "validated_result": row[16],
            "source_inspection_id": row[17],
            "page_count": row[18],
        }

    def _build_candidate(
        self, *, cursor: _Cursor, row: Mapping[str, object]
    ) -> CanonicalCandidate:
        payload = cast(Mapping[str, object], row["representation_payload"])
        source_lineage = cast(Mapping[str, object], payload.get("source_lineage", {}))
        elements = self._load_elements(
            cursor=cursor,
            tenant_id=str(row["tenant_id"]),
            canonical_representation_id=UUID(str(row["canonical_representation_id"])),
        )
        graph = CanonicalGraph(
            content_hash=_compute_graph_hash(
                CanonicalGraph(
                    content_hash="placeholder",
                    payload=dict(payload),
                    elements=elements,
                    source_lineage=dict(source_lineage),
                )
            ),
            payload=dict(payload),
            elements=elements,
            source_lineage=dict(source_lineage),
        )
        return CanonicalCandidate(
            canonical_representation_id=UUID(str(row["canonical_representation_id"])),
            provider_result_id=UUID(str(row["provider_result_id"])),
            source_artifact_id=UUID(str(row["source_artifact_id"])),
            document_version_id=UUID(str(row["document_version_id"])),
            canonical_schema_version=str(row["canonical_schema_version"]),
            assembly_policy_version=str(row["assembly_policy_version"]),
            graph=graph,
        )

    def _load_elements(
        self,
        *,
        cursor: _Cursor,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> tuple[CanonicalElement, ...]:
        cursor.execute(
            """
            SELECT element.stable_key, element.element_type, element.page_number,
                   element.reading_order, element.observed_value,
                   element.normalized_value, element.uncertainty,
                   element.parent_element_id, region.region_payload,
                   element.canonical_element_id
              FROM document_ai_canonical_elements AS element
              LEFT JOIN document_ai_source_regions AS region
                ON region.tenant_id = element.tenant_id
               AND region.canonical_element_id = element.canonical_element_id
             WHERE element.tenant_id = %s
               AND element.canonical_representation_id = %s
             ORDER BY element.ordinal ASC""",
            (tenant_id, canonical_representation_id),
        )
        rows = cursor.fetchall()
        elements: list[CanonicalElement] = []
        seen_regions: set[UUID] = set()
        for row in rows:
            region_payload = cast(Mapping[str, object], row[8] or {}) if row[8] is not None else {}
            source_region_raw = region_payload.get("source_region", {})
            source_region = (
                dict(source_region_raw) if isinstance(source_region_raw, Mapping) else {}
            )
            lineage_raw = region_payload.get("element_lineage", {})
            lineage = dict(lineage_raw) if isinstance(lineage_raw, Mapping) else {}
            parent_element_id = row[7]
            if parent_element_id is not None:
                lineage["parent_element_id"] = str(parent_element_id)
            canonical_element_id = UUID(str(row[9]))
            lineage["canonical_element_id"] = str(canonical_element_id)
            if canonical_element_id in seen_regions:
                raise CanonicalValidationError(
                    "canonical_duplicate_element_region",
                    retryable=False,
                    message="A canonical element has more than one source region row.",
                )
            seen_regions.add(canonical_element_id)
            elements.append(
                CanonicalElement(
                    stable_key=str(row[0]),
                    element_type=str(row[1]),
                    page_number=int(row[2]),
                    reading_order=int(row[3]),
                    observed_value=dict(row[4]) if isinstance(row[4], Mapping) else None,
                    normalized_value=dict(row[5]) if isinstance(row[5], Mapping) else None,
                    uncertainty=dict(row[6]) if isinstance(row[6], Mapping) else {},
                    source_region=source_region,
                    lineage=lineage,
                )
            )
        return tuple(elements)

    def _apply_authority_checks(
        self,
        *,
        cursor: _Cursor,
        row: Mapping[str, object],
        result: CanonicalValidationResult,
        expected_scope_ids: tuple[str, ...],
    ) -> CanonicalValidationResult:
        reasons = set(result.reasons)
        provider_result = cast(Mapping[str, object], row["validated_result"])
        provider_scope_ids = _provider_scope_ids(provider_result)
        if provider_scope_ids != expected_scope_ids:
            reasons.add("canonical_provider_scope_coverage_invalid")
        if _has_parent_cycle(
            cursor=cursor,
            tenant_id=str(row["tenant_id"]),
            canonical_representation_id=UUID(str(row["canonical_representation_id"])),
        ):
            reasons.add("canonical_parent_cycle_detected")
        reasons.update(
            _validate_parent_relationships(
                cursor=cursor,
                tenant_id=str(row["tenant_id"]),
                canonical_representation_id=UUID(str(row["canonical_representation_id"])),
            )
        )
        if reasons == set(result.reasons):
            return result
        report = dict(result.validation_report)
        report["reason_codes"] = sorted(reasons)
        report["provider_scope_ids"] = list(provider_scope_ids)
        report["expected_scope_ids"] = list(expected_scope_ids)
        missing_pages = result.missing_pages
        if reasons:
            return CanonicalValidationResult(
                state="rejected",
                readiness="none",
                missing_pages=missing_pages,
                reasons=tuple(sorted(reasons)),
                validation_report=report,
            )
        return CanonicalValidationResult(
            state="validated",
            readiness="full",
            missing_pages=missing_pages,
            reasons=(),
            validation_report=report,
        )

    def _persist_validation_result(
        self,
        *,
        cursor: _Cursor,
        tenant_id: str,
        canonical_representation_id: UUID,
        result: CanonicalValidationResult,
    ) -> None:
        state = "validated" if result.state == "validated" else "rejected"
        readiness_state = "full" if result.state == "validated" else "none"
        timestamp_column = "validated_at" if result.state == "validated" else "rejected_at"
        other_timestamp_column = "rejected_at" if result.state == "validated" else "validated_at"
        cursor.execute(
            f"""
            UPDATE document_ai_canonical_representations
               SET state = %s,
                   readiness_state = %s,
                   canonical_validation_version = %s,
                   validation_report = %s::jsonb,
                   {timestamp_column} = now(),
                   {other_timestamp_column} = NULL
             WHERE tenant_id = %s
               AND canonical_representation_id = %s
            """,
            (
                state,
                readiness_state,
                CANONICAL_VALIDATION_VERSION,
                json.dumps(result.validation_report, sort_keys=True),
                tenant_id,
                canonical_representation_id,
            ),
        )
        if result.state == "validated":
            cursor.execute(
                """
                INSERT INTO document_ai_processing_outbox (
                    tenant_id, processing_operation_id, processing_work_item_id,
                    event_type, payload, routing_key, correlation_id
                )
                SELECT representation.tenant_id, representation.processing_operation_id,
                       work.processing_work_item_id, %s,
                       jsonb_build_object(
                           'canonical_representation_id',
                               representation.canonical_representation_id,
                           'document_version_id', representation.document_version_id,
                           'provider_result_id', representation.provider_result_id,
                           'source_artifact_id', representation.source_artifact_id,
                           'validation_version',
                               representation.canonical_validation_version
                       ),
                       'document_ai.processing', operation.correlation_id
                  FROM document_ai_canonical_representations AS representation
                  JOIN document_ai_processing_operations AS operation
                    ON operation.tenant_id = representation.tenant_id
                   AND operation.processing_operation_id = representation.processing_operation_id
                  JOIN document_ai_processing_work_items AS work
                    ON work.tenant_id = operation.tenant_id
                   AND work.processing_operation_id = operation.processing_operation_id
                 WHERE representation.tenant_id = %s
                   AND representation.canonical_representation_id = %s
                ON CONFLICT (tenant_id, processing_operation_id, event_type) DO NOTHING
                """,
                (CANONICAL_VALIDATION_CONTINUATION_EVENT, tenant_id, canonical_representation_id),
            )

    def _reconcile_validation_result(
        self,
        *,
        connection: object,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> CanonicalValidationResult | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT validation_report, state, readiness_state
                  FROM document_ai_canonical_representations
                 WHERE tenant_id = %s
                   AND canonical_representation_id = %s
                """,
                (tenant_id, canonical_representation_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        report = dict(row[0]) if isinstance(row[0], Mapping) else {}
        return CanonicalValidationResult(
            state="validated" if str(row[1]) == "validated" else "rejected",
            readiness="full" if str(row[2]) == "full" else "none",
            missing_pages=tuple(int(page) for page in report.get("missing_pages", [])),
            reasons=tuple(report.get("reason_codes", [])),
            validation_report=report,
        )

    def _reconstruct_existing_result(
        self, *, row: Mapping[str, object]
    ) -> CanonicalValidationResult | None:
        version = str(row["canonical_validation_version"] or "")
        state = str(row["state"])
        readiness = str(row["readiness_state"])
        if version != CANONICAL_VALIDATION_VERSION or state not in {"validated", "rejected"}:
            return None
        report = row["validation_report"]
        if not isinstance(report, Mapping):
            report = {}
        report_map = dict(report)
        return CanonicalValidationResult(
            state="validated" if state == "validated" else "rejected",
            readiness="full" if readiness == "full" else "none",
            missing_pages=tuple(int(page) for page in report_map.get("missing_pages", [])),
            reasons=tuple(report_map.get("reason_codes", [])),
            validation_report=report_map,
        )

    def _ensure_continuation_outbox(
        self,
        *,
        cursor: _Cursor,
        row: Mapping[str, object],
        validation_version: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO document_ai_processing_outbox (
                tenant_id, processing_operation_id, processing_work_item_id,
                event_type, payload, routing_key, correlation_id
            )
            SELECT representation.tenant_id, representation.processing_operation_id,
                   work.processing_work_item_id, %s,
                   jsonb_build_object(
                       'canonical_representation_id', representation.canonical_representation_id,
                       'document_version_id', representation.document_version_id,
                       'provider_result_id', representation.provider_result_id,
                       'source_artifact_id', representation.source_artifact_id,
                       'validation_version', %s
                   ),
                   'document_ai.processing', operation.correlation_id
              FROM document_ai_canonical_representations AS representation
              JOIN document_ai_processing_operations AS operation
                ON operation.tenant_id = representation.tenant_id
               AND operation.processing_operation_id = representation.processing_operation_id
              JOIN document_ai_processing_work_items AS work
                ON work.tenant_id = operation.tenant_id
               AND work.processing_operation_id = operation.processing_operation_id
             WHERE representation.tenant_id = %s
               AND representation.canonical_representation_id = %s
            ON CONFLICT (tenant_id, processing_operation_id, event_type) DO NOTHING
            """,
            (
                CANONICAL_VALIDATION_CONTINUATION_EVENT,
                validation_version,
                str(row["tenant_id"]),
                row["canonical_representation_id"],
            ),
        )


def _validate_source_lineage(
    *,
    reasons: list[str],
    lineage: Mapping[str, object],
    expected_source_artifact_id: UUID,
    expected_document_version_id: UUID | None,
    expected_provider_result_id: UUID | None,
    expected_processing_operation_id: UUID | None,
) -> None:
    expected_values = {
        "source_artifact_id": expected_source_artifact_id,
        "document_version_id": expected_document_version_id,
        "provider_result_id": expected_provider_result_id,
        "processing_operation_id": expected_processing_operation_id,
    }
    for key, expected in expected_values.items():
        if expected is None:
            continue
        raw = lineage.get(key)
        if not isinstance(raw, str):
            reasons.append(f"canonical_{key}_mismatch")
            continue
        try:
            if UUID(raw) != expected:
                reasons.append(f"canonical_{key}_mismatch")
        except ValueError:
            reasons.append(f"canonical_{key}_mismatch")


def _validate_structural_units(
    *, structural_units: Sequence[object], reasons: list[str]
) -> set[int]:
    pages: set[int] = set()
    for unit in structural_units:
        if not isinstance(unit, Mapping):
            reasons.append("canonical_structural_units_invalid")
            continue
        typed_unit = cast(Mapping[str, object], unit)
        if typed_unit.get("kind") != "page":
            reasons.append("canonical_structural_units_invalid")
            continue
        page_number = typed_unit.get("page_number")
        if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
            reasons.append("canonical_structural_units_invalid")
            continue
        pages.add(page_number)
    if len(pages) != len(structural_units):
        reasons.append("canonical_structural_units_invalid")
    return pages


def _validate_page_reading_orders(
    *, page_groups: Mapping[int, list[CanonicalElement]], reasons: list[str]
) -> None:
    for elements in page_groups.values():
        reading_orders = [element.reading_order for element in elements]
        if reading_orders != list(range(len(elements))):
            reasons.append("canonical_reading_order_invalid")


def _detect_parent_cycle(elements: Sequence[CanonicalElement]) -> bool:
    parent_map: dict[str, str] = {}
    keys = {
        str(element.lineage.get("canonical_element_id"))
        for element in elements
        if isinstance(element.lineage.get("canonical_element_id"), str)
    }
    for element in elements:
        parent_id = element.lineage.get("parent_element_id")
        if isinstance(parent_id, str) and parent_id in keys:
            element_id = element.lineage.get("canonical_element_id")
            if isinstance(element_id, str):
                parent_map[element_id] = parent_id
    for start in parent_map:
        seen: set[str] = set()
        current = start
        while current in parent_map:
            if current in seen:
                return True
            seen.add(current)
            current = parent_map[current]
    return False


def _validate_parent_relationships(
    *,
    cursor: _Cursor,
    tenant_id: str,
    canonical_representation_id: UUID,
) -> tuple[str, ...]:
    cursor.execute(
        """
        SELECT canonical_element_id, parent_element_id, ordinal
          FROM document_ai_canonical_elements
         WHERE tenant_id = %s
           AND canonical_representation_id = %s
        """,
        (tenant_id, canonical_representation_id),
    )
    rows = cursor.fetchall()
    known_ids = {str(row[0]) for row in rows}
    ordinals = {str(row[0]): int(row[2]) for row in rows}
    parent_map: dict[str, str] = {}
    reasons: set[str] = set()
    for row in rows:
        element_id = str(row[0])
        parent_id = row[1]
        if parent_id is None:
            continue
        parent_id_str = str(parent_id)
        if parent_id_str == element_id:
            reasons.add("canonical_parent_self_reference")
            continue
        if parent_id_str not in known_ids:
            reasons.add("canonical_parent_missing")
            continue
        parent_map[element_id] = parent_id_str
        if ordinals[parent_id_str] >= ordinals[element_id]:
            reasons.add("canonical_parent_order_invalid")
    for start in parent_map:
        seen: set[str] = set()
        current = start
        while current in parent_map:
            if current in seen:
                reasons.add("canonical_parent_cycle_detected")
                break
            seen.add(current)
            current = parent_map[current]
    return tuple(sorted(reasons))


def _element_sort_key(element: CanonicalElement) -> tuple[int, int, str]:
    return (element.page_number, element.reading_order, element.stable_key)


def _compute_graph_hash(graph: CanonicalGraph) -> str:
    hash_elements = [
        {
            "stable_key": element.stable_key,
            "element_type": element.element_type,
            "page_number": element.page_number,
            "reading_order": element.reading_order,
            "observed_value": element.observed_value,
            "normalized_value": element.normalized_value,
            "uncertainty": element.uncertainty,
            "source_region": element.source_region,
            **({"lineage": element.lineage} if element.lineage else {}),
        }
        for element in graph.elements
    ]
    return compute_canonical_hash({"payload": graph.payload, "elements": hash_elements}).sha256_hex


def _expected_pages_from_provider_result(provider_result: object) -> tuple[int, ...]:
    payload = cast(Mapping[str, object], provider_result)
    result = cast(Mapping[str, object], payload.get("result", {}))
    pages = result.get("pages", [])
    if not isinstance(pages, list):
        return ()
    page_numbers: list[int] = []
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        page_number = page.get("page_number")
        if isinstance(page_number, int) and not isinstance(page_number, bool):
            page_numbers.append(page_number)
    return tuple(sorted(dict.fromkeys(page_numbers)))


def _expected_structural_scope_ids(
    *, cursor: _Cursor, row: Mapping[str, object]
) -> tuple[str, ...]:
    cursor.execute(
        """
        SELECT structural_scope_id
          FROM document_ai_structural_scopes
         WHERE tenant_id = %s
           AND source_inspection_id = %s
         ORDER BY scope_ordinal ASC
        """,
        (str(row["tenant_id"]), row["source_inspection_id"]),
    )
    return tuple(str(result[0]) for result in cursor.fetchall())


def _provider_scope_ids(provider_result: Mapping[str, object]) -> tuple[str, ...]:
    scope_ids = provider_result.get("structural_scope_ids", ())
    if not isinstance(scope_ids, list):
        return ()
    return tuple(str(scope_id) for scope_id in scope_ids if isinstance(scope_id, str))


def _same_tenant_and_generation(*, cursor: _Cursor, row: Mapping[str, object]) -> bool:
    cursor.execute(
        """
        SELECT 1
          FROM document_ai_canonical_representations AS representation
          JOIN document_ai_document_versions AS version
            ON version.tenant_id = representation.tenant_id
           AND version.document_version_id = representation.document_version_id
          JOIN document_ai_source_artifacts AS artifact
            ON artifact.tenant_id = representation.tenant_id
           AND artifact.document_version_id = representation.document_version_id
         WHERE representation.tenant_id = %s
           AND representation.canonical_representation_id = %s
           AND version.tenant_id = %s
           AND artifact.tenant_id = %s
        """,
        (
            str(row["tenant_id"]),
            row["canonical_representation_id"],
            str(row["tenant_id"]),
            str(row["tenant_id"]),
        ),
    )
    return cursor.fetchone() is not None


def _has_parent_cycle(
    *, cursor: _Cursor, tenant_id: str, canonical_representation_id: UUID
) -> bool:
    cursor.execute(
        """
        SELECT element.canonical_element_id, element.parent_element_id
          FROM document_ai_canonical_elements AS element
         WHERE element.tenant_id = %s
           AND element.canonical_representation_id = %s
        """,
        (tenant_id, canonical_representation_id),
    )
    rows = cursor.fetchall()
    parent_map = {
        str(row[0]): str(row[1])
        for row in rows
        if row[1] is not None and isinstance(row[1], UUID)
    }
    for start in parent_map:
        seen: set[str] = set()
        current = start
        while current in parent_map:
            if current in seen:
                return True
            seen.add(current)
            current = parent_map[current]
    return False
