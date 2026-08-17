"""Deterministic Document AI evidence resolution over hybrid retrieval candidates.

This module takes retrieval candidates, validates current document authority,
groups structurally related chunks into evidence items, preserves source
lineage, and records conflicts explicitly without deciding which side is true.
"""

from __future__ import annotations

from uuid import UUID
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Literal
from collections import defaultdict
from dataclasses import dataclass
from collections.abc import Mapping
from collections.abc import Iterable
from collections.abc import Sequence

import psycopg
from pydantic import Field
from pydantic import BaseModel
from pydantic import ConfigDict

from shared.determinism.input_hash import compute_canonical_hash
from shared.workflow_evidence_projection import SourceReference
from services.document_ai.app.hybrid_retrieval import HybridRetrievalCandidate
from services.document_ai.app.persistence_support import connect_document_ai_database

EVIDENCE_RESOLUTION_VERSION = "v1"
_MAX_EVIDENCE_ITEMS = 100


class EvidenceConflictRecord(BaseModel):
    """Represent one deterministic conflict group preserved for downstream use."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    conflict_id: str = Field(min_length=1)
    evidence_item_ids: tuple[str, ...] = Field(min_length=2)
    canonical_element_ids: tuple[str, ...] = Field(min_length=1)
    state: Literal["open", "resolved", "dismissed"] = "open"
    reason_code: str = Field(min_length=1)
    detail: dict[str, object] = Field(default_factory=dict)


class ResolvedEvidenceItem(BaseModel):
    """Represent one bounded evidence item with exact provenance lineage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_item_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_version_id: str = Field(min_length=1)
    canonical_representation_id: str = Field(min_length=1)
    canonical_element_ids: tuple[str, ...] = Field(default_factory=tuple)
    canonical_element_keys: tuple[str, ...] = Field(default_factory=tuple)
    source_references: tuple[SourceReference, ...] = Field(default_factory=tuple)
    retrieval_chunk_ids: tuple[str, ...] = Field(default_factory=tuple)
    retrieval_methods: tuple[str, ...] = Field(default_factory=tuple)
    fusion_rank: int = Field(ge=1)
    fusion_score: float
    evidence_state: Literal["current", "historical", "stale"] = "current"
    correction_state: Literal["original", "corrected", "mixed"] = "original"
    conflict_state: Literal["none", "conflicted"] = "none"
    effective_value: object | None = None
    provenance: dict[str, object] = Field(default_factory=dict)


class ResolvedEvidenceSet(BaseModel):
    """Represent a bounded set of evidence items and their explicit conflicts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_set_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    document_ids: tuple[str, ...] = Field(default_factory=tuple)
    active_document_version_ids: tuple[str, ...] = Field(default_factory=tuple)
    evidence_items: tuple[ResolvedEvidenceItem, ...] = Field(default_factory=tuple)
    conflicts: tuple[EvidenceConflictRecord, ...] = Field(default_factory=tuple)
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)


@dataclass(frozen=True)
class _CandidateFacts:
    tenant_id: str
    candidate: HybridRetrievalCandidate
    source_partition_key: str
    source_location_label: str
    semantic_key: tuple[str, ...]
    canonical_element_ids: tuple[str, ...]
    evidence_state: Literal["current", "historical", "stale"]
    effective_value: object | None
    correction_state: Literal["original", "corrected", "mixed"]
    source_reference: SourceReference


class DocumentAIEvidenceResolutionRepository:
    """Resolve hybrid retrieval candidates into provenance-safe evidence sets."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def resolve_hybrid_candidates(
        self,
        *,
        tenant_id: str,
        candidates: Sequence[HybridRetrievalCandidate],
        include_historical: bool = False,
        limit: int = _MAX_EVIDENCE_ITEMS,
    ) -> ResolvedEvidenceSet:
        ordered_candidates = _dedupe_hybrid_candidates(candidates)
        if limit < 1:
            raise ValueError("evidence_limit_required")
        if not ordered_candidates:
            evidence_set_id = _stable_evidence_set_id(
                tenant_id=tenant_id,
                document_ids=(),
                active_document_version_ids=(),
                evidence_item_ids=(),
            )
            return ResolvedEvidenceSet(
                evidence_set_id=evidence_set_id,
                tenant_id=tenant_id,
                diagnostics=("no_candidates_supplied",),
            )

        document_ids = tuple(sorted({str(item.document_id) for item in ordered_candidates}))
        with connect_document_ai_database(self._database_url) as connection:
            active_versions = _load_active_document_versions(
                connection=connection, tenant_id=tenant_id, document_ids=document_ids
            )
            canonical_elements = _load_canonical_elements(
                connection=connection,
                tenant_id=tenant_id,
                candidates=ordered_candidates,
            )
            effective_values = _load_effective_values(
                connection=connection,
                tenant_id=tenant_id,
                canonical_element_ids=tuple(sorted(canonical_elements.values())),
            )
            corrections = _load_corrections(
                connection=connection,
                tenant_id=tenant_id,
                canonical_element_ids=tuple(sorted(canonical_elements.values())),
            )

        facts: list[_CandidateFacts] = []
        diagnostics: list[str] = []
        for candidate in ordered_candidates:
            active_document_version_id = active_versions.get(str(candidate.document_id))
            if active_document_version_id is None:
                diagnostics.append("document_missing_from_authority_scope")
                continue
            if str(candidate.document_version_id) != active_document_version_id:
                diagnostics.append("inactive_document_version_excluded")
                if not include_historical:
                    continue
            semantic_key = tuple(sorted(candidate.canonical_element_keys))
            canonical_element_ids = tuple(
                sorted(
                    {
                        canonical_elements[
                            (str(candidate.canonical_representation_id), key)
                        ]
                        for key in semantic_key
                        if (str(candidate.canonical_representation_id), key)
                        in canonical_elements
                    }
                )
            )
            if not canonical_element_ids:
                diagnostics.append("missing_canonical_element_authority")
                continue
            source_partition_key = _source_partition_key(candidate)
            source_location_label = _render_source_location(candidate.source_location)
            source_reference = SourceReference(
                document_id=str(candidate.document_id),
                document_version_id=str(candidate.document_version_id),
                source_location=source_location_label,
            )
            effective_value, correction_state = _resolve_effective_value(
                canonical_element_ids=canonical_element_ids,
                effective_values=effective_values,
                corrections=corrections,
            )
            facts.append(
                _CandidateFacts(
                    tenant_id=tenant_id,
                    candidate=candidate,
                    source_partition_key=source_partition_key,
                    source_location_label=source_location_label,
                    semantic_key=semantic_key,
                    canonical_element_ids=canonical_element_ids,
                    evidence_state=(
                        "historical"
                        if str(candidate.document_version_id) != active_document_version_id
                        else "current"
                    ),
                    effective_value=effective_value,
                    correction_state=correction_state,
                    source_reference=source_reference,
                )
            )

        grouped: dict[tuple[object, ...], list[_CandidateFacts]] = defaultdict(list)
        for fact in facts:
            grouped[_evidence_group_key(fact)].append(fact)

        resolved_items = [
            _build_resolved_item(group)
            for group in sorted(
            grouped.values(),
                key=lambda group: (
                    min(item.candidate.fusion_rank for item in group),
                    min(str(item.candidate.document_id) for item in group),
                    min(item.candidate.chunk_key for item in group),
                    min(str(item.candidate.retrieval_chunk_id) for item in group),
                ),
            )
        ]

        resolved_items = resolved_items[:limit]
        conflict_records = _build_conflict_records(resolved_items=resolved_items)
        conflicted_item_ids = {
            item_id
            for conflict in conflict_records
            for item_id in conflict.evidence_item_ids
        }
        resolved_items = [
            item.model_copy(
                update={
                "conflict_state": (
                    "conflicted" if item.evidence_item_id in conflicted_item_ids else "none"
                )
                }
            )
            for item in resolved_items
        ]

        evidence_set_id = _stable_evidence_set_id(
            tenant_id=tenant_id,
            document_ids=tuple(sorted({item.document_id for item in resolved_items})),
            active_document_version_ids=tuple(
                sorted({item.document_version_id for item in resolved_items})
            ),
            evidence_item_ids=tuple(item.evidence_item_id for item in resolved_items),
        )
        return ResolvedEvidenceSet(
            evidence_set_id=evidence_set_id,
            tenant_id=tenant_id,
            document_ids=tuple(sorted({item.document_id for item in resolved_items})),
            active_document_version_ids=tuple(
                sorted({item.document_version_id for item in resolved_items})
            ),
            evidence_items=tuple(resolved_items),
            conflicts=tuple(conflict_records),
            diagnostics=tuple(dict.fromkeys(diagnostics)),
        )


def resolve_hybrid_retrieval_candidates(
    *,
    database_url: str,
    tenant_id: str,
    candidates: Sequence[HybridRetrievalCandidate],
    include_historical: bool = False,
    limit: int = _MAX_EVIDENCE_ITEMS,
) -> ResolvedEvidenceSet:
    """Resolve hybrid retrieval candidates using a one-shot repository wrapper."""

    repository = DocumentAIEvidenceResolutionRepository(database_url=database_url)
    return repository.resolve_hybrid_candidates(
        tenant_id=tenant_id,
        candidates=candidates,
        include_historical=include_historical,
        limit=limit,
    )


def _dedupe_hybrid_candidates(
    candidates: Sequence[HybridRetrievalCandidate],
) -> list[HybridRetrievalCandidate]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.fusion_rank,
            item.document_id,
            item.chunk_key,
            item.retrieval_chunk_id,
        ),
    )
    seen: set[UUID] = set()
    deduped: list[HybridRetrievalCandidate] = []
    for candidate in ordered:
        if candidate.retrieval_chunk_id in seen:
            continue
        seen.add(candidate.retrieval_chunk_id)
        deduped.append(candidate)
    return deduped


def _load_active_document_versions(
    *,
    connection: psycopg.Connection[object],
    tenant_id: str,
    document_ids: tuple[str, ...],
) -> dict[str, str]:
    if not document_ids:
        return {}
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT document_id, active_document_version_id
              FROM document_ai_documents
             WHERE tenant_id = %s
               AND document_id = ANY(%s::uuid[])
            """,
            (tenant_id, list(document_ids)),
        )
        rows = cursor.fetchall()
    return {
        str(row[0]): str(row[1])
        for row in rows
        if row[1] is not None
    }


def _load_canonical_elements(
    *,
    connection: psycopg.Connection[object],
    tenant_id: str,
    candidates: Sequence[HybridRetrievalCandidate],
) -> dict[tuple[str, str], str]:
    stable_keys = sorted(
        {
            key
            for candidate in candidates
            for key in candidate.canonical_element_keys
            if key
        }
    )
    if not stable_keys:
        return {}
    canonical_representation_ids = sorted(
        {str(candidate.canonical_representation_id) for candidate in candidates}
    )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT canonical_representation_id, stable_key, canonical_element_id
              FROM document_ai_canonical_elements
             WHERE tenant_id = %s
               AND canonical_representation_id = ANY(%s::uuid[])
               AND stable_key = ANY(%s::text[])
            """,
            (tenant_id, canonical_representation_ids, stable_keys),
        )
        rows = cursor.fetchall()
    return {
        (str(row[0]), str(row[1])): str(row[2])
        for row in rows
        if row[0] is not None and row[1] is not None
    }


def _load_effective_values(
    *,
    connection: psycopg.Connection[object],
    tenant_id: str,
    canonical_element_ids: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    if not canonical_element_ids:
        return {}
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT canonical_element_id, source_observed_value, original_interpreted_value,
                   corrected_value, effective_value, active_correction_id, correction_state
              FROM document_ai_effective_values
             WHERE tenant_id = %s
               AND canonical_element_id = ANY(%s::uuid[])
            """,
            (tenant_id, list(canonical_element_ids)),
        )
        rows = cursor.fetchall()
    return {
        str(row[0]): {
            "source_observed_value": row[1],
            "original_interpreted_value": row[2],
            "corrected_value": row[3],
            "effective_value": row[4],
            "active_correction_id": None if row[5] is None else str(row[5]),
            "correction_state": str(row[6]),
        }
        for row in rows
    }


def _load_corrections(
    *,
    connection: psycopg.Connection[object],
    tenant_id: str,
    canonical_element_ids: tuple[str, ...],
) -> dict[str, list[dict[str, object]]]:
    if not canonical_element_ids:
        return {}
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT correction_id, canonical_element_id, correction_state, corrected_value,
                   source_observed_value, original_interpreted_value, effective_value
              FROM document_ai_corrections
             WHERE tenant_id = %s
               AND canonical_element_id = ANY(%s::uuid[])
             ORDER BY created_at ASC, correction_id ASC
            """,
            (tenant_id, list(canonical_element_ids)),
        )
        rows = cursor.fetchall()
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[1])].append(
            {
                "correction_id": str(row[0]),
                "correction_state": str(row[2]),
                "corrected_value": row[3],
                "source_observed_value": row[4],
                "original_interpreted_value": row[5],
                "effective_value": row[6],
            }
        )
    return grouped


def _resolve_effective_value(
    *,
    canonical_element_ids: tuple[str, ...],
    effective_values: Mapping[str, dict[str, object]],
    corrections: Mapping[str, list[dict[str, object]]],
) -> tuple[object | None, Literal["original", "corrected", "mixed"]]:
    values: list[object] = []
    states: set[str] = set()
    for canonical_element_id in canonical_element_ids:
        effective = effective_values.get(canonical_element_id)
        if effective is None:
            effective = {}
        states.add(str(effective.get("correction_state", "original")))
        value = effective.get("effective_value")
        if value is None:
            value = effective.get("corrected_value")
        correction_entries = corrections.get(canonical_element_id, [])
        if correction_entries:
            if any(str(item["correction_state"]) == "active" for item in correction_entries):
                states.add("corrected")
            if value is None:
                for item in correction_entries:
                    candidate_value = item.get("effective_value")
                    if candidate_value is None:
                        candidate_value = item.get("corrected_value")
                    if candidate_value is not None:
                        value = candidate_value
                        break
            states.update(
                str(item["correction_state"])
                for item in correction_entries
                if str(item["correction_state"]) != "active"
            )
        if value is not None:
            values.append(value)
    if not values:
        state: Literal["original", "corrected", "mixed"] = "original"
        return None, state
    if len({compute_canonical_hash(value).sha256_hex for value in values}) > 1:
        return values[0], "mixed"
    state = "corrected" if any(item == "corrected" for item in states) else "original"
    return values[0], state


def _evidence_group_key(fact: _CandidateFacts) -> tuple[object, ...]:
    return (
        fact.candidate.document_id,
        fact.candidate.document_version_id,
        fact.candidate.canonical_representation_id,
        fact.semantic_key,
        fact.source_partition_key,
    )


def _build_resolved_item(group: Sequence[_CandidateFacts]) -> ResolvedEvidenceItem:
    ordered = sorted(
        group,
        key=lambda item: (
            item.candidate.fusion_rank,
            item.candidate.chunk_key,
            item.candidate.retrieval_chunk_id,
        ),
    )
    first = ordered[0]
    semantic_key = first.semantic_key
    canonical_element_ids = first.canonical_element_ids
    source_references = _unique_source_references(item.source_reference for item in ordered)
    retrieval_chunk_ids = tuple(
        dict.fromkeys(str(item.candidate.retrieval_chunk_id) for item in ordered)
    )
    retrieval_methods = tuple(
        dict.fromkeys(method for item in ordered for method in item.candidate.retrieval_methods)
    )
    evidence_item_id = _stable_evidence_item_id(
        tenant_id=first.tenant_id,
        document_id=str(first.candidate.document_id),
        document_version_id=str(first.candidate.document_version_id),
        canonical_representation_id=str(first.candidate.canonical_representation_id),
        semantic_key=semantic_key,
        source_partition_key=first.source_partition_key,
        retrieval_chunk_ids=retrieval_chunk_ids,
    )
    provenance = {
        "policy_version": EVIDENCE_RESOLUTION_VERSION,
        "source_partition_key": first.source_partition_key,
        "source_location": first.source_location_label,
        "canonical_element_keys": list(semantic_key),
        "candidate_count": len(group),
        "retrieval_chunk_ids": list(retrieval_chunk_ids),
        "retrieval_methods": list(retrieval_methods),
        "source_lineage": [dict(item.candidate.source_lineage) for item in ordered],
        "fusion_scores": [item.candidate.fusion_score for item in ordered],
        "fusion_ranks": [item.candidate.fusion_rank for item in ordered],
    }
    return ResolvedEvidenceItem(
        evidence_item_id=evidence_item_id,
        document_id=str(first.candidate.document_id),
        document_version_id=str(first.candidate.document_version_id),
        canonical_representation_id=str(first.candidate.canonical_representation_id),
        canonical_element_ids=canonical_element_ids,
        canonical_element_keys=semantic_key,
        source_references=source_references,
        retrieval_chunk_ids=retrieval_chunk_ids,
        retrieval_methods=retrieval_methods,
        fusion_rank=min(item.candidate.fusion_rank for item in ordered),
        fusion_score=max(item.candidate.fusion_score for item in ordered),
        evidence_state=first.evidence_state,
        correction_state=_merge_correction_state(item.correction_state for item in ordered),
        effective_value=first.effective_value,
        provenance=provenance,
    )


def _build_conflict_records(
    *,
    resolved_items: Sequence[ResolvedEvidenceItem],
) -> list[EvidenceConflictRecord]:
    by_semantic_key: dict[tuple[str, ...], list[ResolvedEvidenceItem]] = defaultdict(list)
    for item in resolved_items:
        by_semantic_key[item.canonical_element_keys].append(item)

    conflicts: list[EvidenceConflictRecord] = []
    for semantic_key, items in by_semantic_key.items():
        if len(items) < 2:
            continue
        value_hashes = {
            compute_canonical_hash(item.effective_value).sha256_hex
            for item in items
            if item.effective_value is not None
        }
        if len(value_hashes) <= 1 and all(item.correction_state == "original" for item in items):
            continue
        conflict_id = _stable_conflict_id(
            evidence_item_ids=tuple(sorted(item.evidence_item_id for item in items)),
            canonical_element_ids=tuple(
                sorted({cid for item in items for cid in item.canonical_element_ids})
            ),
        )
        conflicts.append(
            EvidenceConflictRecord(
                conflict_id=conflict_id,
                evidence_item_ids=tuple(sorted(item.evidence_item_id for item in items)),
                canonical_element_ids=tuple(
                    sorted({cid for item in items for cid in item.canonical_element_ids})
                ),
                reason_code="conflicting_evidence_sources",
                detail={
                    "semantic_key": list(semantic_key),
                    "evidence_item_ids": [item.evidence_item_id for item in items],
                    "effective_values": [item.effective_value for item in items],
                    "source_references": [
                        [reference.model_dump() for reference in item.source_references]
                        for item in items
                    ],
                },
            )
        )
    return sorted(conflicts, key=lambda item: (item.conflict_id, item.reason_code))


def _merge_correction_state(
    states: Iterable[Literal["original", "corrected", "mixed"]]
) -> Literal["original", "corrected", "mixed"]:
    unique = set(states)
    if not unique:
        return "original"
    if len(unique) > 1:
        return "mixed"
    return next(iter(unique))


def _unique_source_references(
    references: Iterable[SourceReference],
) -> tuple[SourceReference, ...]:
    seen: set[tuple[str, str | None, str | None]] = set()
    ordered: list[SourceReference] = []
    for reference in references:
        key = (
            reference.document_id,
            reference.document_version_id,
            reference.source_location,
        )
        if key in seen:
            continue
        seen.add(key)
        ordered.append(reference)
    return tuple(ordered)


def _render_source_location(source_location: Mapping[str, object]) -> str:
    parts: list[str] = []
    page_number = source_location.get("page_number")
    if isinstance(page_number, int):
        parts.append(f"page:{page_number}")
    sheet_name = source_location.get("sheet_name")
    if isinstance(sheet_name, str) and sheet_name.strip():
        parts.append(f"sheet:{sheet_name.strip()}")
    slide_number = source_location.get("slide_number")
    if isinstance(slide_number, int):
        parts.append(f"slide:{slide_number}")
    line_start = source_location.get("line_start")
    line_end = source_location.get("line_end")
    if isinstance(line_start, int) or isinstance(line_end, int):
        parts.append(
            f"lines:{line_start if isinstance(line_start, int) else '?'}-"
            f"{line_end if isinstance(line_end, int) else '?'}"
        )
    row_start = source_location.get("row_start")
    row_end = source_location.get("row_end")
    if isinstance(row_start, int) or isinstance(row_end, int):
        parts.append(
            f"rows:{row_start if isinstance(row_start, int) else '?'}-"
            f"{row_end if isinstance(row_end, int) else '?'}"
        )
    cell_reference = source_location.get("cell_reference")
    if isinstance(cell_reference, str) and cell_reference.strip():
        parts.append(f"cell:{cell_reference.strip()}")
    if not parts:
        return "source:unknown"
    return " ".join(parts)


def _source_partition_key(candidate: HybridRetrievalCandidate) -> str:
    structural_context = candidate.structural_context
    if isinstance(structural_context, Mapping):
        value = structural_context.get("source_partition_key")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return compute_canonical_hash(
        {
            "document_id": str(candidate.document_id),
            "document_version_id": str(candidate.document_version_id),
            "canonical_representation_id": str(candidate.canonical_representation_id),
            "source_location": candidate.source_location,
        }
    ).sha256_hex


def _stable_evidence_item_id(
    *,
    tenant_id: str,
    document_id: str,
    document_version_id: str,
    canonical_representation_id: str,
    semantic_key: tuple[str, ...],
    source_partition_key: str,
    retrieval_chunk_ids: tuple[str, ...],
) -> str:
    payload = {
        "tenant_id": tenant_id,
        "document_id": document_id,
        "document_version_id": document_version_id,
        "canonical_representation_id": canonical_representation_id,
        "semantic_key": list(semantic_key),
        "source_partition_key": source_partition_key,
        "retrieval_chunk_ids": list(retrieval_chunk_ids),
    }
    return str(uuid5(NAMESPACE_URL, f"kodi://document-ai/evidence-item/{compute_canonical_hash(payload).sha256_hex}"))


def _stable_conflict_id(
    *,
    evidence_item_ids: tuple[str, ...],
    canonical_element_ids: tuple[str, ...],
) -> str:
    payload = {
        "evidence_item_ids": list(evidence_item_ids),
        "canonical_element_ids": list(canonical_element_ids),
    }
    return str(uuid5(NAMESPACE_URL, f"kodi://document-ai/evidence-conflict/{compute_canonical_hash(payload).sha256_hex}"))


def _stable_evidence_set_id(
    *,
    tenant_id: str,
    document_ids: tuple[str, ...],
    active_document_version_ids: tuple[str, ...],
    evidence_item_ids: tuple[str, ...],
) -> str:
    payload = {
        "tenant_id": tenant_id,
        "document_ids": list(document_ids),
        "active_document_version_ids": list(active_document_version_ids),
        "evidence_item_ids": list(evidence_item_ids),
    }
    return str(uuid5(NAMESPACE_URL, f"kodi://document-ai/evidence-set/{compute_canonical_hash(payload).sha256_hex}"))
