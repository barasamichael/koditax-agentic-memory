from __future__ import annotations

from copy import deepcopy
from typing import cast
from typing import TypedDict
from datetime import date
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.knowledge.app.main import create_app
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeIngestionRecord
from services.knowledge.app.repository import KnowledgeRepositoryError
from services.knowledge.app.repository import INVALID_KNOWLEDGE_LINEAGE
from services.knowledge.app.repository import INVALID_KNOWLEDGE_REQUEST
from services.knowledge.app.repository import KnowledgeAnchorDetailRecord
from services.knowledge.app.repository import KnowledgeChunkSummaryRecord
from services.knowledge.app.repository import KnowledgeSourceDetailRecord
from services.knowledge.app.repository import KnowledgeSourceSummaryRecord
from services.knowledge.app.repository import KNOWLEDGE_IDEMPOTENCY_CONFLICT
from services.knowledge.app.repository import KnowledgeIngestionDetailRecord
from services.knowledge.app.repository import KnowledgeIngestionSummaryRecord
from services.knowledge.app.repository import KnowledgeBulkIngestionItemRecord
from services.knowledge.app.repository import KnowledgeBulkOperationItemRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.knowledge.app.repository import KnowledgeSourceVersionLifecycleRecord

ALLOWED_METADATA_CORRECTION_FIELDS = {
    "title",
    "issuing_authority",
    "point_in_time_url",
    "tax_year",
}

ObjectDict = dict[str, object]


class IngestionJobState(TypedDict):
    document_id: str
    requested_by: str
    ingestion_state: str
    source_input_origin: str
    source_input_ref: str
    payload_checksum_sha256: str
    source_class: str | None
    extracted_metadata: ObjectDict
    proposed_source_record: ObjectDict
    review_notes: list[ObjectDict]
    created_at: str
    completed_at: str | None


class SourceVersionState(TypedDict):
    source_version_id: str
    source_id: str
    source_family_id: str
    publication_state: str
    source_input_origin: str
    source_input_ref: str
    source_version_form: str
    effective_from: str
    effective_to: str | None
    tax_year: int | None
    supersedes_source_version_id: str | None
    superseded_by_source_version_id: str | None
    title: str
    source_class: str
    tax_domain: str
    authority_level: str
    issuing_authority: str
    canonical_url: str
    created_at: str
    retired_at: str | None
    anchors: list[ObjectDict]


def _as_object_dict(value: object) -> ObjectDict:
    assert isinstance(value, dict)
    return deepcopy(cast(ObjectDict, value))


class StubKnowledgeRepository:
    def __init__(self) -> None:
        self._records = (
            KnowledgeSearchRecord(
                source_id="KNW-ITA-15-2",
                title="Income Tax Act (Cap. 470), Section 15(2)",
                url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
                source_type="tax_law",
                tax_domain="income_tax",
                authority_level="statute",
                effective_from="1974-01-01",
                effective_to=None,
                tax_year=None,
                anchor_id="income-tax-act-15-2",
                content="Allowable deductions in production of income under section 15(2).",
            ),
            KnowledgeSearchRecord(
                source_id="KNW-ITA-5-1-B",
                title="Income Tax Act (Cap. 470), Section 5(1)(b)",
                url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
                source_type="tax_law",
                tax_domain="income_tax",
                authority_level="statute",
                effective_from="1974-01-01",
                effective_to=None,
                tax_year=None,
                anchor_id="income-tax-act-5-1-b",
                content="Chargeability for non-resident employment income under section 5(1)(b).",
            ),
        )
        self._ingestion_jobs: dict[str, IngestionJobState] = {}
        self._source_versions: dict[str, SourceVersionState] = {}
        self._ingestion_created_counter = 0
        self._source_created_counter = 0

    def _next_ingestion_created_at(self) -> str:
        self._ingestion_created_counter += 1
        return f"2026-05-03T00:00:{self._ingestion_created_counter:02d}+00:00"

    def _next_ingestion_job_id(self, kind: str) -> str:
        return f"job-{kind}-{self._ingestion_created_counter + 1:03d}"

    def _next_document_id(self, kind: str) -> str:
        return f"doc-{kind}-{self._ingestion_created_counter + 1:03d}"

    def _next_source_created_at(self) -> str:
        self._source_created_counter += 1
        return f"2026-05-03T01:00:{self._source_created_counter:02d}+00:00"

    def _source_versions_sorted(self) -> list[SourceVersionState]:
        return sorted(
            self._source_versions.values(),
            key=lambda record: (
                str(record["source_family_id"]),
                str(record["effective_from"]),
                str(record["effective_to"] or "9999-12-31"),
                str(record["source_version_id"]),
            ),
        )

    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (query, source_type, tax_domain, effective_date)
        return self._records

    def retrieve_records(
        self,
        *,
        source_ids: tuple[str, ...],
        anchor_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        return tuple(
            record
            for record in self._records
            if record.source_id in source_ids or record.anchor_id in anchor_ids
        )

    def timeline_search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str,
        start_date: date,
        end_date: date,
    ) -> tuple[KnowledgeTimelineRecord, ...]:
        _ = (query, source_type, tax_domain, start_date, end_date)
        return (
            KnowledgeTimelineRecord(
                source_id="KNW-ITA-15-2",
                source_version_id="123e4567-e89b-12d3-a456-426614174100",
                anchor_id="income-tax-act-15-2-2025",
                title="Income Tax Act (Cap. 470), Section 15(2)",
                url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
                source_type="tax_law",
                authority_level="statute",
                tax_domain="income_tax",
                effective_from="2025-01-01",
                effective_to="2025-12-31",
                publication_state="superseded",
                timeline_position=1,
                content="Allowable deductions before the 2026 effective window.",
            ),
            KnowledgeTimelineRecord(
                source_id="KNW-ITA-15-2",
                source_version_id="123e4567-e89b-12d3-a456-426614174101",
                anchor_id="income-tax-act-15-2-2026",
                title="Income Tax Act (Cap. 470), Section 15(2)",
                url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
                source_type="tax_law",
                authority_level="statute",
                tax_domain="income_tax",
                effective_from="2026-01-01",
                effective_to=None,
                publication_state="published",
                timeline_position=2,
                content="Allowable deductions for the current effective window.",
            ),
        )

    def _store_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        document_id: str,
        requested_by: str,
        source_input_origin: str,
        source_input_ref: str,
        payload_checksum_sha256: str,
        source_class: str | None,
        extracted_metadata: ObjectDict,
        proposed_source_record: ObjectDict,
    ) -> KnowledgeIngestionRecord:
        created_at = self._next_ingestion_created_at()
        self._ingestion_jobs[ingestion_job_id] = {
            "document_id": document_id,
            "requested_by": requested_by,
            "ingestion_state": "uploaded",
            "source_input_origin": source_input_origin,
            "source_input_ref": source_input_ref,
            "payload_checksum_sha256": payload_checksum_sha256,
            "source_class": source_class,
            "extracted_metadata": deepcopy(extracted_metadata),
            "proposed_source_record": deepcopy(proposed_source_record),
            "review_notes": [],
            "created_at": created_at,
            "completed_at": None,
        }
        return KnowledgeIngestionRecord(
            ingestion_job_id=ingestion_job_id,
            document_id=document_id,
            requested_by=requested_by,
            ingestion_state="uploaded",
            source_input_origin=source_input_origin,
            source_input_ref=source_input_ref,
            payload_checksum_sha256=payload_checksum_sha256,
            source_class=source_class,
        )

    def _ingestion_detail_record(self, ingestion_job_id: str) -> KnowledgeIngestionDetailRecord:
        stored = self._ingestion_jobs[ingestion_job_id]
        return KnowledgeIngestionDetailRecord(
            ingestion_job_id=ingestion_job_id,
            document_id=str(stored["document_id"]),
            requested_by=str(stored["requested_by"]),
            ingestion_state=str(stored["ingestion_state"]),
            source_input_origin=str(stored["source_input_origin"]),
            source_input_ref=str(stored["source_input_ref"]),
            payload_checksum_sha256=str(stored["payload_checksum_sha256"]),
            source_class=(
                stored["source_class"] if isinstance(stored["source_class"], str) else None
            ),
            extracted_metadata=deepcopy(stored["extracted_metadata"]),
            proposed_source_record=deepcopy(stored["proposed_source_record"]),
            review_notes=tuple(deepcopy(item) for item in stored["review_notes"]),
            completed_at=(
                stored["completed_at"] if isinstance(stored["completed_at"], str) else None
            ),
        )

    def ingest_file_source(
        self,
        *,
        requested_by: str,
        idempotency_key: str,
        filename: str,
        mime_type: str,
        file_content_base64: str,
        source_input_origin: str | None,
        source_class: str | None,
        legacy_import_acknowledged: bool,
    ) -> KnowledgeIngestionRecord:
        _ = (idempotency_key, file_content_base64)
        if not legacy_import_acknowledged:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message=(
                    "Knowledge direct file ingestion requires explicit legacy import "
                    "acknowledgement."
                ),
            )
        next_job_id = self._next_ingestion_job_id("file")
        next_document_id = self._next_document_id("file")
        return self._store_ingestion_job(
            ingestion_job_id=next_job_id,
            document_id=next_document_id,
            requested_by=requested_by,
            source_input_origin=source_input_origin or "official_source_upload",
            source_input_ref=f"official-source-upload://sha256/{next_job_id}",
            payload_checksum_sha256=f"{next_job_id}-sha256",
            source_class=source_class or "tax_law",
            extracted_metadata={"filename": filename, "mime_type": mime_type},
            proposed_source_record={"ingestion_kind": "file"},
        )

    def ingest_registered_document_source(
        self,
        *,
        requested_by: str,
        idempotency_key: str,
        document_id: str,
        storage_key: str,
        mime_type: str,
        payload_checksum_sha256: str,
        source_document_system: str,
        source_input_origin: str | None,
        source_class: str | None,
    ) -> KnowledgeIngestionRecord:
        _ = (idempotency_key, mime_type)
        if source_document_system != "storage_registered":
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge request field `source_document_system` is invalid.",
            )
        lowered_storage_key = storage_key.lower()
        if (
            lowered_storage_key.startswith("http://")
            or lowered_storage_key.startswith("https://")
            or "://" in storage_key
        ):
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_LINEAGE,
                message="Knowledge document storage must use a local storage key, not a URL.",
            )
        if idempotency_key == "document-lineage-conflict":
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_LINEAGE,
                message="Knowledge ingestion document storage reference is invalid.",
            )
        next_job_id = self._next_ingestion_job_id("document")
        return self._store_ingestion_job(
            ingestion_job_id=next_job_id,
            document_id=document_id,
            requested_by=requested_by,
            source_input_origin=source_input_origin or "official_source_upload",
            source_input_ref=(
                f"official-source-upload://{source_document_system}/documents/{document_id}"
            ),
            payload_checksum_sha256=payload_checksum_sha256,
            source_class=source_class or "tax_law",
            extracted_metadata={"storage_key": storage_key, "mime_type": mime_type},
            proposed_source_record={"ingestion_kind": "document"},
        )

    def ingest_url_source(
        self,
        *,
        requested_by: str,
        idempotency_key: str,
        url: str,
        source_input_origin: str | None,
        source_class: str | None,
    ) -> KnowledgeIngestionRecord:
        if idempotency_key == "conflict-url-key":
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_IDEMPOTENCY_CONFLICT,
                message="Knowledge ingestion idempotency key conflicts with existing payload.",
            )
        next_job_id = self._next_ingestion_job_id("url")
        next_document_id = self._next_document_id("url")
        return self._store_ingestion_job(
            ingestion_job_id=next_job_id,
            document_id=next_document_id,
            requested_by=requested_by,
            source_input_origin=source_input_origin or "official_source_url",
            source_input_ref=f"official-source-url://{url}",
            payload_checksum_sha256=f"{next_job_id}-sha256",
            source_class=source_class or "guidance",
            extracted_metadata={"normalized_url": url},
            proposed_source_record={"ingestion_kind": "url"},
        )

    def bulk_ingest_file_sources(
        self,
        *,
        requested_by: str,
        items: tuple[dict[str, object], ...],
    ) -> tuple[KnowledgeBulkIngestionItemRecord, ...]:
        _ = (requested_by, items)
        raise AssertionError("bulk ingestion is outside the current tests/knowledge slice")

    def bulk_ingest_registered_document_sources(
        self,
        *,
        requested_by: str,
        items: tuple[dict[str, object], ...],
    ) -> tuple[KnowledgeBulkIngestionItemRecord, ...]:
        _ = (requested_by, items)
        raise AssertionError("bulk ingestion is outside the current tests/knowledge slice")

    def bulk_ingest_url_sources(
        self,
        *,
        requested_by: str,
        items: tuple[dict[str, object], ...],
    ) -> tuple[KnowledgeBulkIngestionItemRecord, ...]:
        _ = (requested_by, items)
        raise AssertionError("bulk ingestion is outside the current tests/knowledge slice")

    def get_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
    ) -> KnowledgeIngestionDetailRecord:
        if ingestion_job_id not in self._ingestion_jobs:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge ingestion job identifier is invalid.",
            )
        return self._ingestion_detail_record(ingestion_job_id)

    def list_ingestion_jobs(
        self,
        *,
        ingestion_state: str | None,
        source_input_origin: str | None,
        source_class: str | None,
        requested_by: str | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[KnowledgeIngestionSummaryRecord, ...]:
        normalized_sort_by = sort_by or "created_at"
        normalized_sort_order = sort_order or "desc"
        if ingestion_state is not None and ingestion_state not in {
            "uploaded",
            "review_pending",
            "approved",
            "rejected",
            "published",
        }:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge management ingestion_state filter is invalid.",
            )
        if source_input_origin is not None and source_input_origin not in {
            "official_source_upload",
            "official_source_url",
        }:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge management source input origin filter is unsupported.",
            )
        if normalized_sort_by not in {"created_at", "completed_at", "ingestion_state"}:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge management sort_by filter is invalid.",
            )
        if normalized_sort_order not in {"asc", "desc"}:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge management sort_order filter is invalid.",
            )

        filtered_records = [
            (ingestion_job_id, stored)
            for ingestion_job_id, stored in self._ingestion_jobs.items()
            if (ingestion_state is None or str(stored["ingestion_state"]) == ingestion_state)
            and (
                source_input_origin is None
                or str(stored["source_input_origin"]) == source_input_origin
            )
            and (source_class is None or stored["source_class"] == source_class)
            and (requested_by is None or str(stored["requested_by"]) == requested_by)
        ]

        def ingestion_sort_value(item: tuple[str, IngestionJobState]) -> str:
            _, record = item
            if normalized_sort_by == "created_at":
                return str(record["created_at"])
            if normalized_sort_by == "completed_at":
                return str(record["completed_at"] or "")
            return str(record["ingestion_state"])

        filtered_records.sort(
            key=lambda item: (ingestion_sort_value(item), str(item[0])),
            reverse=normalized_sort_order == "desc",
        )
        page = filtered_records[offset : offset + limit]
        return tuple(
            KnowledgeIngestionSummaryRecord(
                ingestion_job_id=ingestion_job_id,
                document_id=str(stored["document_id"]),
                requested_by=str(stored["requested_by"]),
                ingestion_state=str(stored["ingestion_state"]),
                source_input_origin=str(stored["source_input_origin"]),
                source_input_ref=str(stored["source_input_ref"]),
                payload_checksum_sha256=str(stored["payload_checksum_sha256"]),
                source_class=(
                    stored["source_class"] if isinstance(stored["source_class"], str) else None
                ),
                created_at=str(stored["created_at"]),
                completed_at=(
                    stored["completed_at"] if isinstance(stored["completed_at"], str) else None
                ),
            )
            for ingestion_job_id, stored in page
        )

    def review_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        reviewed_by: str,
        review_notes: tuple[dict[str, object], ...],
        proposed_source_updates: dict[str, object] | None,
    ) -> KnowledgeIngestionDetailRecord:
        stored = self._ingestion_jobs[ingestion_job_id]
        stored["ingestion_state"] = "review_pending"
        proposed: ObjectDict = deepcopy(stored["proposed_source_record"])
        if proposed_source_updates is not None:
            proposed.update(proposed_source_updates)
        proposed["last_reviewed_by"] = reviewed_by
        stored["proposed_source_record"] = proposed
        stored["review_notes"] = [deepcopy(item) for item in review_notes]
        return self._ingestion_detail_record(ingestion_job_id)

    def approve_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        reviewed_by: str,
        publication_payload: dict[str, object],
        review_notes: tuple[dict[str, object], ...],
    ) -> KnowledgeIngestionDetailRecord:
        stored = self._ingestion_jobs[ingestion_job_id]
        if str(stored["ingestion_state"]) not in {"uploaded", "review_pending", "rejected"}:
            raise KnowledgeRepositoryError(
                reason_code="invalid_publication_state_transition",
                message=(
                    "Knowledge ingestion job cannot transition to approval from its current state."
                ),
            )
        if stored["source_class"] is not None and str(stored["source_class"]) != str(
            publication_payload["source_class"]
        ):
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_LINEAGE,
                message=(
                    "Knowledge publication source class conflicts with ingested lineage metadata."
                ),
            )
        proposed: ObjectDict = deepcopy(stored["proposed_source_record"])
        proposed["publication_payload"] = deepcopy(publication_payload)
        proposed["approved_by"] = reviewed_by
        stored["proposed_source_record"] = proposed
        stored["review_notes"] = [deepcopy(item) for item in review_notes]
        stored["ingestion_state"] = "approved"
        return self._ingestion_detail_record(ingestion_job_id)

    def reject_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        reviewed_by: str,
        review_notes: tuple[dict[str, object], ...],
    ) -> KnowledgeIngestionDetailRecord:
        stored = self._ingestion_jobs[ingestion_job_id]
        if str(stored["ingestion_state"]) == "published":
            raise KnowledgeRepositoryError(
                reason_code="invalid_publication_state_transition",
                message="Published knowledge ingestion jobs cannot be rejected.",
            )
        proposed: ObjectDict = deepcopy(stored["proposed_source_record"])
        proposed["rejected_by"] = reviewed_by
        stored["proposed_source_record"] = proposed
        stored["review_notes"] = [deepcopy(item) for item in review_notes]
        stored["ingestion_state"] = "rejected"
        stored["completed_at"] = "2026-05-03T00:00:00+00:00"
        return self._ingestion_detail_record(ingestion_job_id)

    def publish_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        published_by: str,
    ) -> KnowledgeIngestionDetailRecord:
        stored = self._ingestion_jobs[ingestion_job_id]
        if str(stored["ingestion_state"]) == "published":
            return self._ingestion_detail_record(ingestion_job_id)
        if str(stored["ingestion_state"]) != "approved":
            raise KnowledgeRepositoryError(
                reason_code="invalid_publication_state_transition",
                message="Knowledge ingestion job must be approved before publication.",
            )
        proposed: ObjectDict = deepcopy(stored["proposed_source_record"])
        approved_by = str(proposed.get("approved_by", "")).strip()
        if not approved_by:
            raise KnowledgeRepositoryError(
                reason_code="knowledge_publication_safety_rejected",
                message="Knowledge publication requires prior reviewer approval metadata.",
            )
        if approved_by == published_by:
            raise KnowledgeRepositoryError(
                reason_code="knowledge_publication_safety_rejected",
                message=(
                    "Knowledge publication requires a publisher distinct "
                    "from the approving reviewer."
                ),
            )
        publication_payload = _as_object_dict(proposed.get("publication_payload", {}))
        source_version_id = str(
            publication_payload.get(
                "source_version_id",
                "123e4567-e89b-12d3-a456-426614174700",
            )
        )
        tax_year_value = publication_payload.get("tax_year")
        source_created_at = self._next_source_created_at()
        source_id = str(publication_payload.get("source_id", "KNW-FINANCE-2026"))
        self._source_versions[source_version_id] = SourceVersionState(
            source_version_id=source_version_id,
            source_id=source_id,
            source_family_id=str(publication_payload.get("source_family_id", "KNW-FINANCE-FAMILY")),
            publication_state="published",
            source_input_origin=str(stored["source_input_origin"]),
            source_input_ref=str(stored["source_input_ref"]),
            source_version_form=str(
                publication_payload.get(
                    "source_version_form",
                    "point_in_time_consolidation",
                )
            ),
            effective_from=str(publication_payload.get("effective_from", "2026-01-01")),
            effective_to=(
                str(publication_payload["effective_to"])
                if isinstance(publication_payload.get("effective_to"), str)
                else None
            ),
            tax_year=tax_year_value if isinstance(tax_year_value, int) else None,
            supersedes_source_version_id=None,
            superseded_by_source_version_id=None,
            title=str(publication_payload.get("title", f"Governed source {source_id}")),
            source_class=str(publication_payload.get("source_class", "tax_law")),
            tax_domain=str(publication_payload.get("tax_domain", "income_tax")),
            authority_level=str(publication_payload.get("authority_level", "statute")),
            issuing_authority=str(
                publication_payload.get("issuing_authority", "Kenya Revenue Authority")
            ),
            canonical_url=str(
                publication_payload.get(
                    "point_in_time_url",
                    f"https://example.com/sources/{source_id}",
                )
            ),
            created_at=source_created_at,
            retired_at=None,
            anchors=[
                _as_object_dict(item)
                for item in cast(list[object], publication_payload.get("anchors", []))
            ],
        )
        proposed["published_source_version_id"] = source_version_id
        proposed["published_by"] = published_by
        stored["proposed_source_record"] = proposed
        stored["ingestion_state"] = "published"
        stored["completed_at"] = "2026-05-03T00:00:00+00:00"
        return self._ingestion_detail_record(ingestion_job_id)

    def bulk_reject_ingestion_jobs(
        self,
        *,
        reviewed_by: str,
        ingestion_job_ids: tuple[str, ...],
        review_notes: tuple[dict[str, object], ...],
    ) -> tuple[KnowledgeBulkOperationItemRecord, ...]:
        _ = (reviewed_by, ingestion_job_ids, review_notes)
        raise AssertionError("bulk lifecycle is outside the current tests/knowledge slice")

    def bulk_publish_ingestion_jobs(
        self,
        *,
        published_by: str,
        ingestion_job_ids: tuple[str, ...],
    ) -> tuple[KnowledgeBulkOperationItemRecord, ...]:
        _ = (published_by, ingestion_job_ids)
        raise AssertionError("bulk lifecycle is outside the current tests/knowledge slice")

    def supersede_source_version(
        self,
        *,
        source_version_id: str,
        successor_source_version_id: str,
        superseded_by: str,
    ) -> KnowledgeSourceVersionLifecycleRecord:
        _ = superseded_by
        predecessor = self._source_versions[source_version_id]
        successor = self._source_versions[successor_source_version_id]
        if predecessor["source_family_id"] != successor["source_family_id"]:
            raise KnowledgeRepositoryError(
                reason_code="knowledge_supersession_conflict",
                message=(
                    "Knowledge supersession requires predecessor and successor from the "
                    "same governed source family."
                ),
            )
        predecessor_from = str(predecessor["effective_from"])
        predecessor_to = str(predecessor["effective_to"] or "")
        successor_from = str(successor["effective_from"])
        if successor_from <= predecessor_from:
            raise KnowledgeRepositoryError(
                reason_code="knowledge_temporal_scope_mismatch",
                message=(
                    "Knowledge supersession successor must begin after the predecessor "
                    "effective window starts."
                ),
            )
        if predecessor_to and successor_from <= predecessor_to:
            raise KnowledgeRepositoryError(
                reason_code="knowledge_temporal_scope_mismatch",
                message=(
                    "Knowledge supersession successor must begin after the predecessor "
                    "effective window ends."
                ),
            )
        if str(predecessor["publication_state"]) not in {"published", "superseded"}:
            raise KnowledgeRepositoryError(
                reason_code="knowledge_record_not_published",
                message="Knowledge source version must be published before supersession.",
            )
        predecessor["publication_state"] = "superseded"
        predecessor["superseded_by_source_version_id"] = successor_source_version_id
        return self.get_source_version_lifecycle(source_version_id=source_version_id)

    def archive_source_version(
        self,
        *,
        source_version_id: str,
        archived_by: str,
    ) -> KnowledgeSourceVersionLifecycleRecord:
        _ = archived_by
        record = self._source_versions[source_version_id]
        if str(record["publication_state"]) == "archived":
            return self.get_source_version_lifecycle(source_version_id=source_version_id)
        if str(record["publication_state"]) not in {"published", "superseded"}:
            raise KnowledgeRepositoryError(
                reason_code="knowledge_record_not_published",
                message=(
                    "Knowledge source version must be published or superseded before archiving."
                ),
            )
        if (
            str(record["publication_state"]) == "published"
            and record["superseded_by_source_version_id"] is None
        ):
            raise KnowledgeRepositoryError(
                reason_code="invalid_publication_state_transition",
                message=(
                    "Knowledge source version cannot be archived while it remains "
                    "the active published version."
                ),
            )
        record["publication_state"] = "archived"
        return self.get_source_version_lifecycle(source_version_id=source_version_id)

    def bulk_archive_source_versions(
        self,
        *,
        archived_by: str,
        source_version_ids: tuple[str, ...],
    ) -> tuple[KnowledgeBulkOperationItemRecord, ...]:
        _ = (archived_by, source_version_ids)
        raise AssertionError("bulk lifecycle is outside the current tests/knowledge slice")

    def get_source_version_lifecycle(
        self,
        *,
        source_version_id: str,
    ) -> KnowledgeSourceVersionLifecycleRecord:
        if source_version_id not in self._source_versions:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge source version identifier is invalid.",
            )
        record = self._source_versions[source_version_id]
        return KnowledgeSourceVersionLifecycleRecord(
            source_version_id=source_version_id,
            source_id=str(record["source_id"]),
            source_family_id=str(record["source_family_id"]),
            publication_state=str(record["publication_state"]),
            source_input_origin=str(record["source_input_origin"]),
            source_version_form=str(record["source_version_form"]),
            effective_from=str(record["effective_from"]),
            effective_to=(
                record["effective_to"] if isinstance(record["effective_to"], str) else None
            ),
            tax_year=record["tax_year"] if isinstance(record["tax_year"], int) else None,
            supersedes_source_version_id=(
                record["supersedes_source_version_id"]
                if isinstance(record["supersedes_source_version_id"], str)
                else None
            ),
            superseded_by_source_version_id=(
                record["superseded_by_source_version_id"]
                if isinstance(record["superseded_by_source_version_id"], str)
                else None
            ),
        )

    def list_source_versions(
        self,
        *,
        publication_state: str | None,
        source_id: str | None,
        source_family_id: str | None,
        tax_domain: str | None,
        source_class: str | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[KnowledgeSourceVersionSummaryRecord, ...]:
        normalized_sort_by = sort_by or "source_family_id"
        normalized_sort_order = sort_order or "asc"
        if publication_state is not None and publication_state not in {
            "published",
            "superseded",
            "archived",
        }:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge management publication_state filter is invalid.",
            )
        if normalized_sort_by not in {"source_family_id", "effective_from", "publication_state"}:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge management sort_by filter is invalid.",
            )
        if normalized_sort_order not in {"asc", "desc"}:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge management sort_order filter is invalid.",
            )

        filtered_records = [
            record
            for record in self._source_versions_sorted()
            if (publication_state is None or str(record["publication_state"]) == publication_state)
            and (source_id is None or str(record["source_id"]) == source_id)
            and (source_family_id is None or str(record["source_family_id"]) == source_family_id)
            and (tax_domain is None or str(record["tax_domain"]) == tax_domain)
            and (source_class is None or str(record["source_class"]) == source_class)
        ]

        def source_version_sort_value(record: SourceVersionState) -> str:
            if normalized_sort_by == "effective_from":
                return str(record["effective_from"])
            if normalized_sort_by == "publication_state":
                return str(record["publication_state"])
            return str(record["source_family_id"])

        filtered_records.sort(
            key=lambda record: (
                source_version_sort_value(record),
                str(record["source_version_id"]),
            ),
            reverse=normalized_sort_order == "desc",
        )
        page = filtered_records[offset : offset + limit]
        return tuple(
            KnowledgeSourceVersionSummaryRecord(
                source_version_id=str(record["source_version_id"]),
                source_id=str(record["source_id"]),
                source_family_id=str(record["source_family_id"]),
                title=str(record["title"]),
                source_class=str(record["source_class"]),
                tax_domain=str(record["tax_domain"]),
                authority_level=str(record["authority_level"]),
                publication_state=str(record["publication_state"]),
                source_input_origin=str(record["source_input_origin"]),
                source_version_form=str(record["source_version_form"]),
                effective_from=str(record["effective_from"]),
                effective_to=(
                    record["effective_to"] if isinstance(record["effective_to"], str) else None
                ),
                tax_year=record["tax_year"] if isinstance(record["tax_year"], int) else None,
                supersedes_source_version_id=(
                    record["supersedes_source_version_id"]
                    if isinstance(record["supersedes_source_version_id"], str)
                    else None
                ),
                superseded_by_source_version_id=(
                    record["superseded_by_source_version_id"]
                    if isinstance(record["superseded_by_source_version_id"], str)
                    else None
                ),
            )
            for record in page
        )

    def list_sources(
        self,
        *,
        source_class: str | None,
        tax_domain: str | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[KnowledgeSourceSummaryRecord, ...]:
        normalized_sort_by = sort_by or "source_family_id"
        normalized_sort_order = sort_order or "asc"
        if normalized_sort_by not in {"source_family_id", "created_at", "title"}:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge management sort_by filter is invalid.",
            )
        if normalized_sort_order not in {"asc", "desc"}:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge management sort_order filter is invalid.",
            )

        grouped: dict[str, list[SourceVersionState]] = {}
        for record in self._source_versions.values():
            grouped.setdefault(str(record["source_id"]), []).append(record)

        source_records: list[KnowledgeSourceSummaryRecord] = []
        for source_id, versions in grouped.items():
            first = sorted(
                versions,
                key=lambda record: (
                    str(record["effective_from"]),
                    str(record["source_version_id"]),
                ),
            )[0]
            if source_class is not None and str(first["source_class"]) != source_class:
                continue
            if tax_domain is not None and str(first["tax_domain"]) != tax_domain:
                continue
            anchor_count = sum(len(cast(list[object], record["anchors"])) for record in versions)
            source_records.append(
                KnowledgeSourceSummaryRecord(
                    source_id=source_id,
                    source_family_id=str(first["source_family_id"]),
                    title=str(first["title"]),
                    canonical_url=str(first["canonical_url"]),
                    source_class=str(first["source_class"]),
                    tax_domain=str(first["tax_domain"]),
                    authority_level=str(first["authority_level"]),
                    issuing_authority=str(first["issuing_authority"]),
                    version_count=len(versions),
                    anchor_count=anchor_count,
                    created_at=str(first["created_at"]),
                    retired_at=(
                        first["retired_at"] if isinstance(first["retired_at"], str) else None
                    ),
                )
            )
        source_records.sort(
            key=lambda record: (
                str(getattr(record, normalized_sort_by)),
                record.source_id,
            ),
            reverse=normalized_sort_order == "desc",
        )
        return tuple(source_records[offset : offset + limit])

    def get_source(
        self,
        *,
        source_id: str,
    ) -> KnowledgeSourceDetailRecord:
        normalized_source_id = source_id.strip()
        if not normalized_source_id:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge source identifier is invalid.",
            )
        versions = [
            record
            for record in self._source_versions_sorted()
            if str(record["source_id"]) == normalized_source_id
        ]
        if not versions:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge source identifier is invalid.",
            )
        first = versions[0]
        version_records = tuple(
            KnowledgeSourceVersionSummaryRecord(
                source_version_id=str(record["source_version_id"]),
                source_id=str(record["source_id"]),
                source_family_id=str(record["source_family_id"]),
                title=str(record["title"]),
                source_class=str(record["source_class"]),
                tax_domain=str(record["tax_domain"]),
                authority_level=str(record["authority_level"]),
                publication_state=str(record["publication_state"]),
                source_input_origin=str(record["source_input_origin"]),
                source_version_form=str(record["source_version_form"]),
                effective_from=str(record["effective_from"]),
                effective_to=(
                    record["effective_to"] if isinstance(record["effective_to"], str) else None
                ),
                tax_year=record["tax_year"] if isinstance(record["tax_year"], int) else None,
                supersedes_source_version_id=(
                    record["supersedes_source_version_id"]
                    if isinstance(record["supersedes_source_version_id"], str)
                    else None
                ),
                superseded_by_source_version_id=(
                    record["superseded_by_source_version_id"]
                    if isinstance(record["superseded_by_source_version_id"], str)
                    else None
                ),
            )
            for record in versions
        )
        has_document_lineage = any(
            str(record["source_input_origin"]) == "official_source_upload"
            and str(record["source_input_ref"]).startswith(
                "official-source-upload://storage_registered/"
            )
            for record in versions
        )
        has_legacy_import_lineage = any(
            str(record["source_input_origin"]) == "official_source_upload"
            and not str(record["source_input_ref"]).startswith(
                "official-source-upload://storage_registered/"
            )
            for record in versions
        )
        has_url_lineage = any(
            str(record["source_input_origin"]) == "official_source_url" for record in versions
        )
        return KnowledgeSourceDetailRecord(
            source_id=normalized_source_id,
            source_family_id=str(first["source_family_id"]),
            title=str(first["title"]),
            canonical_url=str(first["canonical_url"]),
            source_class=str(first["source_class"]),
            tax_domain=str(first["tax_domain"]),
            authority_level=str(first["authority_level"]),
            issuing_authority=str(first["issuing_authority"]),
            version_count=len(versions),
            anchor_count=sum(len(record["anchors"]) for record in versions),
            chunk_count=sum(
                len(cast(list[object], anchor_state.get("chunks", [])))
                for record in versions
                for anchor_state in record["anchors"]
            ),
            created_at=str(first["created_at"]),
            retired_at=first["retired_at"] if isinstance(first["retired_at"], str) else None,
            versions=version_records,
            retention_summary={
                "lineage_preserved": True,
                "has_document_lineage": has_document_lineage,
                "has_purged_document_lineage": False,
                "has_historical_compatibility_lineage": False,
                "has_legacy_import_lineage": has_legacy_import_lineage,
                "has_url_lineage": has_url_lineage,
                "retention_policy_code": "knowledge-shared-corpus-retention-v1",
                "purge_supported": False,
            },
        )

    def get_anchor(
        self,
        *,
        anchor_id: str,
    ) -> KnowledgeAnchorDetailRecord:
        normalized_anchor_id = anchor_id.strip()
        if not normalized_anchor_id:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge anchor identifier is invalid.",
            )
        for record in self._source_versions_sorted():
            for anchor_state in record["anchors"]:
                if str(anchor_state.get("anchor_id")) != normalized_anchor_id:
                    continue
                raw_anchor_chunks = anchor_state.get("chunks", [])
                assert isinstance(raw_anchor_chunks, list)
                anchor_chunks = cast(list[object], raw_anchor_chunks)
                chunk_records = tuple(
                    KnowledgeChunkSummaryRecord(
                        chunk_id=f"{normalized_anchor_id}-chunk-{index}",
                        chunk_index=index,
                        has_embedding=True,
                    )
                    for index, _ in enumerate(anchor_chunks)
                )
                return KnowledgeAnchorDetailRecord(
                    anchor_id=normalized_anchor_id,
                    source_id=str(record["source_id"]),
                    source_family_id=str(record["source_family_id"]),
                    source_version_id=str(record["source_version_id"]),
                    source_title=str(record["title"]),
                    source_type=str(record["source_class"]),
                    tax_domain=str(record["tax_domain"]),
                    authority_level=str(record["authority_level"]),
                    publication_state=str(record["publication_state"]),
                    anchor_title=str(anchor_state.get("anchor_title", normalized_anchor_id)),
                    anchor_path=str(anchor_state.get("anchor_path", normalized_anchor_id)),
                    temporal_scope_from=str(
                        anchor_state.get("temporal_scope_from", record["effective_from"])
                    ),
                    temporal_scope_to=(
                        str(anchor_state["temporal_scope_to"])
                        if isinstance(anchor_state.get("temporal_scope_to"), str)
                        else None
                    ),
                    chunk_count=len(chunk_records),
                    chunks=chunk_records,
                )
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge anchor identifier is invalid.",
        )

    def correct_ingestion_metadata(
        self,
        *,
        ingestion_job_id: str,
        corrected_by: str,
        review_notes: tuple[dict[str, object], ...],
        publication_payload_updates: dict[str, object],
    ) -> KnowledgeIngestionDetailRecord:
        stored = self._ingestion_jobs[ingestion_job_id]
        if str(stored["ingestion_state"]) not in {
            "review_pending",
            "approved",
            "approved_for_publication",
        }:
            raise KnowledgeRepositoryError(
                reason_code="invalid_publication_state_transition",
                message=(
                    "Knowledge metadata correction is allowed only for editable "
                    "unpublished review-stage material."
                ),
            )
        invalid_fields = sorted(
            field_name
            for field_name in publication_payload_updates
            if field_name not in ALLOWED_METADATA_CORRECTION_FIELDS
        )
        if invalid_fields:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_LINEAGE,
                message="Knowledge metadata correction contains immutable lineage fields.",
            )
        proposed: ObjectDict = deepcopy(stored["proposed_source_record"])
        publication_payload = _as_object_dict(proposed.get("publication_payload", {}))
        publication_payload.update(publication_payload_updates)
        proposed["publication_payload"] = publication_payload
        proposed["last_corrected_by"] = corrected_by
        stored["proposed_source_record"] = proposed
        stored["review_notes"] = [deepcopy(item) for item in review_notes]
        return self._ingestion_detail_record(ingestion_job_id)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(repository=StubKnowledgeRepository())
    with TestClient(app) as test_client:
        yield test_client
