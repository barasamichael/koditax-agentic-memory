"""Durable, fenced persistence for validated OpenAI provider artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from typing import Literal

from shared.determinism.input_hash import compute_canonical_hash

from services.document_ai.app.governed_openai import ValidatedProviderResult
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction
from services.document_ai.app.processing_workers import ProcessingAttemptLease


@dataclass(frozen=True)
class ProviderResultReservationDetails:
    """Describe one logical provider operation before the external call starts."""

    request_fingerprint: str
    model: str
    processing_policy_version: str
    prompt_version: str
    canonical_schema_version: str
    document_version_id: str
    source_artifact_id: str
    source_scope_id: str
    structural_scope_ids: tuple[str, ...]
    source_checksum_sha256: str
    source_size_bytes: int


@dataclass(frozen=True)
class ProviderResultReservation:
    """Represent the durable logical state for one provider operation."""

    reservation_id: str
    reservation_state: Literal["reserved", "in_progress", "completed", "blocked"]
    reservation_generation: int
    request_fingerprint: str
    result_reference: str | None
    provider_request_id: str | None
    provider_response_id: str | None
    can_call_provider: bool


class ProviderResultRepository:
    """Persist provider output once, scoped to the current fenced processing attempt."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def existing_result_reference(self, *, lease: ProcessingAttemptLease) -> str | None:
        """Resolve a prior durable result before repeating an uncertain provider call."""

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT provider_result_id
                       FROM document_ai_provider_results
                      WHERE tenant_id = %s AND processing_operation_id = %s
                        AND provider_result_state = 'validated'
                      ORDER BY created_at ASC LIMIT 1""",
                    (lease.tenant_id, lease.processing_operation_id),
                )
                row = cursor.fetchone()
        return None if row is None else f"provider-result:{row[0]}"

    def reserve(
        self,
        *,
        lease: ProcessingAttemptLease,
        details: ProviderResultReservationDetails,
    ) -> ProviderResultReservation:
        """Durably reserve the logical provider operation before the OpenAI call."""

        if not details.request_fingerprint.strip():
            raise ValueError("provider_result_request_fingerprint_required")
        row = execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.provider_result.reserve",
            transaction_callback=lambda cursor: self._reserve_with_cursor(
                cursor=cursor,
                lease=lease,
                details=details,
            ),
            reconcile_ambiguous_result=lambda connection: self._reconcile_reservation(
                connection=connection,
                lease=lease,
                details=details,
            ),
        )
        if row is None:
            raise RuntimeError("provider_result_reservation_unavailable")
        return self._reservation_from_row(row)

    def mark_in_progress(
        self,
        *,
        lease: ProcessingAttemptLease,
        reservation: ProviderResultReservation,
    ) -> ProviderResultReservation:
        """Advance a reserved provider operation to an in-progress state."""

        row = execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.provider_result.mark_in_progress",
            transaction_callback=lambda cursor: self._mark_in_progress_with_cursor(
                cursor=cursor,
                lease=lease,
                reservation=reservation,
            ),
            reconcile_ambiguous_result=lambda connection: self._reconcile_reservation(
                connection=connection,
                lease=lease,
                details=None,
            ),
        )
        if row is None:
            raise RuntimeError("provider_result_mark_in_progress_unavailable")
        return self._reservation_from_row(row)

    def persist(
        self,
        *,
        lease: ProcessingAttemptLease,
        details: ProviderResultReservationDetails,
        reservation: ProviderResultReservation,
        result: ValidatedProviderResult,
    ) -> str | None:
        """Return an idempotent internal reference, or None for a stale/cancelled attempt."""

        if result.processing_operation_id != str(
            lease.processing_operation_id
        ) or result.processing_attempt_id != str(lease.processing_attempt_id):
            raise ValueError("provider_result_attempt_mismatch")
        if details.request_fingerprint != self._build_request_fingerprint(
            lease=lease, details=details
        ):
            raise ValueError("provider_result_request_fingerprint_mismatch")
        row = execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.provider_result.persist",
            transaction_callback=lambda cursor: self._persist_with_cursor(
                cursor=cursor,
                lease=lease,
                details=details,
                reservation=reservation,
                result=result,
            ),
            reconcile_ambiguous_result=lambda connection: self._reconcile_persist_result(
                connection=connection,
                lease=lease,
                details=details,
            ),
        )
        return f"provider-result:{row[0]}" if row is not None else None

    def _reserve_with_cursor(
        self,
        *,
        cursor: Any,
        lease: ProcessingAttemptLease,
        details: ProviderResultReservationDetails,
    ) -> tuple[object, ...] | None:
        existing_result = self._load_existing_result_row(cursor=cursor, lease=lease)
        if existing_result is not None:
            return (
                "",
                lease.tenant_id,
                str(lease.processing_operation_id),
                str(lease.processing_attempt_id),
                str(lease.processing_work_item_id),
                details.document_version_id,
                details.source_artifact_id,
                "openai",
                details.model,
                details.processing_policy_version,
                details.prompt_version,
                details.canonical_schema_version,
                details.source_scope_id,
                details.request_fingerprint,
                list(details.structural_scope_ids),
                details.source_checksum_sha256,
                details.source_size_bytes,
                "completed",
                0,
                None,
                None,
                None,
                None,
                None,
                None,
                existing_result[0],
                None,
                None,
                None,
                None,
                None,
            )

        reservation = self._load_reservation_row(cursor=cursor, lease=lease)
        if reservation is None:
            return self._insert_reservation(cursor=cursor, lease=lease, details=details)

        if str(reservation[17]) == "completed":
            completed = self._ensure_completed_result(
                cursor=cursor, lease=lease, details=details, reservation_row=reservation
            )
            if completed is not None:
                refreshed = self._load_reservation_row(cursor=cursor, lease=lease)
                if refreshed is not None and refreshed[25] is not None:
                    return refreshed
                return (
                    "",
                    lease.tenant_id,
                    str(lease.processing_operation_id),
                    str(lease.processing_attempt_id),
                    str(lease.processing_work_item_id),
                    details.document_version_id,
                    details.source_artifact_id,
                    "openai",
                    details.model,
                    details.processing_policy_version,
                    details.prompt_version,
                    details.canonical_schema_version,
                    details.source_scope_id,
                    details.request_fingerprint,
                    list(details.structural_scope_ids),
                    details.source_checksum_sha256,
                    details.source_size_bytes,
                    "completed",
                    0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    completed[0],
                    None,
                    None,
                    None,
                    None,
                    None,
                )

        if str(reservation[17]) in {"reserved", "in_progress"}:
            expires_at = reservation[19]
            same_attempt = str(reservation[3]) == str(lease.processing_attempt_id)
            if not same_attempt and (expires_at is None or expires_at > self._now(cursor)):
                return self._blocked_row(reservation)
            return self._touch_reservation(
                cursor=cursor, lease=lease, details=details, current_row=reservation
            )

        return self._insert_reservation(cursor=cursor, lease=lease, details=details)

    def _mark_in_progress_with_cursor(
        self,
        *,
        cursor: Any,
        lease: ProcessingAttemptLease,
        reservation: ProviderResultReservation,
    ) -> tuple[object, ...] | None:
        cursor.execute(
            """
            UPDATE document_ai_provider_result_reservations
               SET reservation_state = 'in_progress',
                   in_progress_at = COALESCE(in_progress_at, now()),
                   updated_at = now()
             WHERE tenant_id = %s
               AND processing_operation_id = %s
               AND reservation_id = %s
               AND processing_attempt_id = %s
               AND reservation_generation = %s
               AND reservation_state IN ('reserved', 'in_progress')
               AND reservation_expires_at > now()
            RETURNING reservation_id
            """,
            (
                lease.tenant_id,
                lease.processing_operation_id,
                reservation.reservation_id,
                lease.processing_attempt_id,
                reservation.reservation_generation,
            ),
        )
        row = cursor.fetchone()
        if row is not None:
            return self._load_reservation_row(cursor=cursor, lease=lease)
        return self._load_reservation_row(cursor=cursor, lease=lease)

    def _persist_with_cursor(
        self,
        *,
        cursor: Any,
        lease: ProcessingAttemptLease,
        details: ProviderResultReservationDetails,
        reservation: ProviderResultReservation,
        result: ValidatedProviderResult,
    ) -> tuple[object, ...] | None:
        reservation_row = self._load_reservation_row(cursor=cursor, lease=lease)
        if reservation_row is None:
            existing = self._load_existing_result_row(cursor=cursor, lease=lease)
            return existing
        if str(reservation_row[17]) == "completed" and reservation_row[25] is not None:
            return (reservation_row[25],)

        cursor.execute(
            """
            SELECT operation.document_version_id, artifact.source_artifact_id,
                   work.processing_work_item_id
              FROM document_ai_processing_work_items AS work
              JOIN document_ai_processing_operations AS operation
                ON operation.tenant_id = work.tenant_id
               AND operation.processing_operation_id = work.processing_operation_id
              JOIN document_ai_processing_attempts AS attempt
                ON attempt.tenant_id = work.tenant_id
               AND attempt.processing_attempt_id = work.current_processing_attempt_id
              JOIN document_ai_source_artifacts AS artifact
                ON artifact.tenant_id = operation.tenant_id
               AND artifact.document_version_id = operation.document_version_id
              JOIN document_ai_source_inspections AS inspection
                ON inspection.tenant_id = operation.tenant_id
               AND inspection.document_version_id = operation.document_version_id
             WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
               AND work.current_processing_attempt_id = %s AND work.fencing_token = %s
               AND work.state = 'leased' AND work.leased_until > now()
               AND attempt.state = 'running'
               AND operation.cancellation_requested_at IS NULL
               AND operation.operation_kind = 'general_document_understanding'
               AND artifact.source_artifact_id::text = %s
               AND artifact.retention_state IN ('active', 'held')
               AND artifact.integrity_state = 'verified'
               AND inspection.policy_version = 'v1'
               AND inspection.disposition = 'accepted'
            """,
            (
                lease.tenant_id,
                lease.processing_work_item_id,
                lease.processing_attempt_id,
                lease.fencing_token,
                details.source_artifact_id,
            ),
        )
        valid = cursor.fetchone()
        if valid is None:
            return None
        cursor.execute(
            """
            INSERT INTO document_ai_provider_results (
                tenant_id, processing_operation_id, processing_attempt_id,
                document_version_id, source_artifact_id, processing_work_item_id,
                provider_name, provider_response_id, provider_request_id,
                request_fingerprint, model_policy, processing_policy_version, prompt_version,
                canonical_schema_version, source_scope_id, provider_result_state,
                validated_result, usage, latency_ms, provider_result_reservation_id
            ) VALUES (%s, %s, %s, %s, %s, %s, 'openai', %s, %s, %s, %s, %s, %s, %s,
                      %s, 'validated', %s::jsonb, %s::jsonb, %s, %s)
            ON CONFLICT (tenant_id, processing_operation_id) DO UPDATE SET
                provider_result_id = document_ai_provider_results.provider_result_id
            RETURNING provider_result_id
            """,
            (
                lease.tenant_id,
                lease.processing_operation_id,
                lease.processing_attempt_id,
                valid[0],
                valid[1],
                valid[2],
                result.provider_response_id,
                result.provider_request_id,
                details.request_fingerprint,
                result.model,
                result.processing_policy_version,
                result.prompt_version,
                result.canonical_schema_version,
                result.source_scope_id,
                json.dumps(result.model_dump(mode="json")),
                json.dumps(result.usage.model_dump(mode="json")),
                result.latency_ms,
                reservation.reservation_id,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            """
            UPDATE document_ai_provider_result_reservations
               SET reservation_state = 'completed',
                   completed_at = COALESCE(completed_at, now()),
                   provider_result_id = %s,
                   provider_request_id = %s,
                   provider_response_id = %s,
                   validated_result = %s::jsonb,
                   usage = %s::jsonb,
                   latency_ms = %s,
                   updated_at = now()
             WHERE tenant_id = %s
               AND processing_operation_id = %s
               AND reservation_id = %s
            """,
            (
                row[0],
                result.provider_request_id,
                result.provider_response_id,
                json.dumps(result.model_dump(mode="json")),
                json.dumps(result.usage.model_dump(mode="json")),
                result.latency_ms,
                lease.tenant_id,
                lease.processing_operation_id,
                reservation.reservation_id,
            ),
        )
        self._queue_canonical_assembly(
            cursor=cursor,
            tenant_id=lease.tenant_id,
            document_version_id=str(valid[0]),
            source_artifact_id=str(valid[1]),
            provider_result_id=str(row[0]),
            correlation_id=lease.correlation_id,
        )
        return row

    def _reconcile_persist_result(
        self,
        *,
        connection: Any,
        lease: ProcessingAttemptLease,
        details: ProviderResultReservationDetails,
    ) -> tuple[object, ...] | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT provider_result_id
                  FROM document_ai_provider_results
                 WHERE tenant_id = %s AND processing_operation_id = %s
                   AND provider_result_state = 'validated'
                """,
                (lease.tenant_id, lease.processing_operation_id),
            )
            row = cursor.fetchone()
            if row is not None:
                return row
            cursor.execute(
                """
                SELECT reservation_id, validated_result, provider_request_id, provider_response_id
                  FROM document_ai_provider_result_reservations
                 WHERE tenant_id = %s AND processing_operation_id = %s
                   AND request_fingerprint = %s
                   AND reservation_state = 'completed'
                """,
                (lease.tenant_id, lease.processing_operation_id, details.request_fingerprint),
            )
            reservation_row = cursor.fetchone()
            if reservation_row is None or reservation_row[1] is None:
                return None
            cursor.execute(
                """
                INSERT INTO document_ai_provider_results (
                    tenant_id, processing_operation_id, processing_attempt_id,
                    document_version_id, source_artifact_id, processing_work_item_id,
                    provider_name, provider_response_id, provider_request_id,
                    request_fingerprint, model_policy, processing_policy_version, prompt_version,
                    canonical_schema_version, source_scope_id, provider_result_state,
                    validated_result, usage, latency_ms, provider_result_reservation_id
                )
                SELECT reservation.tenant_id, reservation.processing_operation_id,
                       reservation.processing_attempt_id, reservation.document_version_id,
                       reservation.source_artifact_id, reservation.processing_work_item_id,
                       'openai', reservation.provider_response_id, reservation.provider_request_id,
                       reservation.request_fingerprint, reservation.model_policy,
                       reservation.processing_policy_version, reservation.prompt_version,
                       reservation.canonical_schema_version, reservation.source_scope_id,
                       'validated', reservation.validated_result,
                       COALESCE(reservation.usage, '{}'::jsonb), COALESCE(reservation.latency_ms, 0),
                       reservation.reservation_id
                  FROM document_ai_provider_result_reservations AS reservation
                 WHERE reservation.tenant_id = %s
                   AND reservation.processing_operation_id = %s
                   AND reservation.reservation_id = %s
                ON CONFLICT (tenant_id, processing_operation_id) DO NOTHING
                RETURNING provider_result_id
                """,
                (lease.tenant_id, lease.processing_operation_id, reservation_row[0]),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return inserted
            cursor.execute(
                """
                SELECT provider_result_id
                  FROM document_ai_provider_results
                 WHERE tenant_id = %s AND processing_operation_id = %s
                   AND provider_result_state = 'validated'
                """,
                (lease.tenant_id, lease.processing_operation_id),
            )
            return cursor.fetchone()

    def _reconcile_reservation(
        self,
        *,
        connection: Any,
        lease: ProcessingAttemptLease,
        details: ProviderResultReservationDetails | None,
    ) -> tuple[object, ...] | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT reservation_id, reservation_generation, reservation_state,
                       request_fingerprint, provider_result_id, provider_request_id,
                       provider_response_id, can_call_provider
                  FROM document_ai_provider_result_reservations
                 WHERE tenant_id = %s AND processing_operation_id = %s
                """,
                (lease.tenant_id, lease.processing_operation_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            if details is not None and str(row[3]) != details.request_fingerprint:
                return None
            if str(row[2]) == "completed" and row[4] is not None:
                return (row[4],)
            if str(row[2]) == "completed" and row[4] is None:
                cursor.execute(
                    """
                    SELECT provider_result_id
                      FROM document_ai_provider_results
                     WHERE tenant_id = %s AND processing_operation_id = %s
                       AND provider_result_state = 'validated'
                    """,
                    (lease.tenant_id, lease.processing_operation_id),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    return existing
            return row

    def _insert_reservation(
        self,
        *,
        cursor: Any,
        lease: ProcessingAttemptLease,
        details: ProviderResultReservationDetails,
    ) -> tuple[object, ...]:
        cursor.execute(
            """
            INSERT INTO document_ai_provider_result_reservations (
                tenant_id, processing_operation_id, processing_attempt_id,
                processing_work_item_id, document_version_id, source_artifact_id,
                provider_name, model_policy, processing_policy_version, prompt_version,
                canonical_schema_version, source_scope_id, request_fingerprint,
                structural_scope_ids, source_checksum_sha256, source_size_bytes,
                reservation_state, reservation_generation, reservation_expires_at,
                reserved_at, updated_at
            )
            SELECT work.tenant_id, work.processing_operation_id, work.current_processing_attempt_id,
                   work.processing_work_item_id, operation.document_version_id,
                   artifact.source_artifact_id, 'openai', %s, %s, %s, %s, %s, %s,
                   %s::jsonb, %s, %s, 'reserved', 1, work.leased_until, now(), now()
              FROM document_ai_processing_work_items AS work
              JOIN document_ai_processing_operations AS operation
                ON operation.tenant_id = work.tenant_id
               AND operation.processing_operation_id = work.processing_operation_id
              JOIN document_ai_processing_attempts AS attempt
                ON attempt.tenant_id = work.tenant_id
               AND attempt.processing_attempt_id = work.current_processing_attempt_id
              JOIN document_ai_source_artifacts AS artifact
                ON artifact.tenant_id = operation.tenant_id
               AND artifact.document_version_id = operation.document_version_id
              JOIN document_ai_source_inspections AS inspection
                ON inspection.tenant_id = operation.tenant_id
               AND inspection.document_version_id = operation.document_version_id
             WHERE work.tenant_id = %s
               AND work.processing_work_item_id = %s
               AND work.current_processing_attempt_id = %s
               AND work.fencing_token = %s
               AND work.state = 'leased'
               AND work.leased_until > now()
               AND attempt.state = 'running'
               AND attempt.fencing_token = %s
               AND operation.operation_kind = 'general_document_understanding'
               AND operation.cancellation_requested_at IS NULL
               AND artifact.source_artifact_id::text = %s
               AND artifact.retention_state IN ('active', 'held')
               AND artifact.integrity_state = 'verified'
               AND inspection.policy_version = 'v1'
               AND inspection.disposition = 'accepted'
            ON CONFLICT (tenant_id, processing_operation_id) DO NOTHING
            RETURNING reservation_id
            """,
            (
                details.model,
                details.processing_policy_version,
                details.prompt_version,
                details.canonical_schema_version,
                details.source_scope_id,
                details.request_fingerprint,
                json.dumps(list(details.structural_scope_ids)),
                details.source_checksum_sha256,
                details.source_size_bytes,
                lease.tenant_id,
                lease.processing_work_item_id,
                lease.processing_attempt_id,
                lease.fencing_token,
                lease.fencing_token,
                details.source_artifact_id,
            ),
        )
        row = cursor.fetchone()
        if row is not None:
            return row
        cursor.execute(
            """
            SELECT reservation_id, reservation_generation, reservation_state,
                   request_fingerprint, provider_result_id, provider_request_id,
                   provider_response_id, can_call_provider
              FROM document_ai_provider_result_reservations
             WHERE tenant_id = %s AND processing_operation_id = %s
            """,
            (lease.tenant_id, lease.processing_operation_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("provider_result_reservation_insert_failed")
        refreshed = self._load_reservation_row(cursor=cursor, lease=lease)
        if refreshed is None:
            raise RuntimeError("provider_result_reservation_refresh_failed")
        return refreshed

    def _touch_reservation(
        self,
        *,
        cursor: Any,
        lease: ProcessingAttemptLease,
        details: ProviderResultReservationDetails,
        current_row: tuple[object, ...],
    ) -> tuple[object, ...]:
        reservation_generation = int(current_row[18]) + 1
        cursor.execute(
            """
            UPDATE document_ai_provider_result_reservations
               SET processing_attempt_id = %s,
                   processing_work_item_id = %s,
                   document_version_id = %s,
                   source_artifact_id = %s,
                   provider_name = 'openai',
                   model_policy = %s,
                   processing_policy_version = %s,
                   prompt_version = %s,
                   canonical_schema_version = %s,
                   source_scope_id = %s,
                   request_fingerprint = %s,
                   structural_scope_ids = %s::jsonb,
                   source_checksum_sha256 = %s,
                   source_size_bytes = %s,
                   reservation_state = 'reserved',
                   reservation_generation = %s,
                   reservation_expires_at = (
                       SELECT leased_until
                         FROM document_ai_processing_work_items
                        WHERE tenant_id = %s AND processing_operation_id = %s
                   ),
                   reserved_at = now(),
                   in_progress_at = NULL,
                   completed_at = NULL,
                   provider_result_id = NULL,
                   provider_request_id = NULL,
                   provider_response_id = NULL,
                   validated_result = NULL,
                   usage = NULL,
                   latency_ms = NULL,
                   updated_at = now()
             WHERE tenant_id = %s
               AND processing_operation_id = %s
               AND reservation_id = %s
            RETURNING reservation_id, reservation_generation, reservation_state,
                      request_fingerprint, provider_result_id, provider_request_id,
                      provider_response_id, can_call_provider
            """,
            (
                lease.processing_attempt_id,
                lease.processing_work_item_id,
                details.document_version_id,
                details.source_artifact_id,
                details.model,
                details.processing_policy_version,
                details.prompt_version,
                details.canonical_schema_version,
                details.source_scope_id,
                details.request_fingerprint,
                json.dumps(list(details.structural_scope_ids)),
                details.source_checksum_sha256,
                details.source_size_bytes,
                reservation_generation,
                lease.tenant_id,
                lease.processing_operation_id,
                lease.tenant_id,
                lease.processing_operation_id,
                current_row[0],
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("provider_result_reservation_update_failed")
        refreshed = self._load_reservation_row(cursor=cursor, lease=lease)
        if refreshed is None:
            raise RuntimeError("provider_result_reservation_refresh_failed")
        return refreshed

    def _ensure_completed_result(
        self,
        *,
        cursor: Any,
        lease: ProcessingAttemptLease,
        details: ProviderResultReservationDetails,
        reservation_row: tuple[object, ...],
    ) -> tuple[object, ...] | None:
        if reservation_row[25] is not None:
            return (reservation_row[25],)
        cursor.execute(
            """
            SELECT provider_result_id
              FROM document_ai_provider_results
             WHERE tenant_id = %s AND processing_operation_id = %s
               AND provider_result_state = 'validated'
            """,
            (lease.tenant_id, lease.processing_operation_id),
        )
        existing = cursor.fetchone()
        if existing is not None:
            return existing
        if reservation_row[26] is None:
            return None
        cursor.execute(
            """
            INSERT INTO document_ai_provider_results (
                tenant_id, processing_operation_id, processing_attempt_id,
                document_version_id, source_artifact_id, processing_work_item_id,
                provider_name, provider_response_id, provider_request_id,
                request_fingerprint, model_policy, processing_policy_version, prompt_version,
                canonical_schema_version, source_scope_id, provider_result_state,
                validated_result, usage, latency_ms, provider_result_reservation_id
            )
            SELECT reservation.tenant_id, reservation.processing_operation_id,
                   reservation.processing_attempt_id, reservation.document_version_id,
                   reservation.source_artifact_id, reservation.processing_work_item_id,
                   'openai', reservation.provider_response_id, reservation.provider_request_id,
                   reservation.request_fingerprint, reservation.model_policy,
                   reservation.processing_policy_version, reservation.prompt_version,
                   reservation.canonical_schema_version, reservation.source_scope_id,
                   'validated', reservation.validated_result,
                   COALESCE(reservation.usage, '{}'::jsonb), COALESCE(reservation.latency_ms, 0),
                   reservation.reservation_id
              FROM document_ai_provider_result_reservations AS reservation
             WHERE reservation.tenant_id = %s
               AND reservation.processing_operation_id = %s
               AND reservation.reservation_id = %s
            ON CONFLICT (tenant_id, processing_operation_id) DO NOTHING
            RETURNING provider_result_id
            """,
            (lease.tenant_id, lease.processing_operation_id, reservation_row[0]),
        )
        inserted = cursor.fetchone()
        if inserted is not None:
            return inserted
        cursor.execute(
            """
            SELECT provider_result_id
              FROM document_ai_provider_results
             WHERE tenant_id = %s AND processing_operation_id = %s
               AND provider_result_state = 'validated'
            """,
            (lease.tenant_id, lease.processing_operation_id),
        )
        return cursor.fetchone()

    def _load_reservation_row(
        self,
        *,
        cursor: Any,
        lease: ProcessingAttemptLease,
    ) -> tuple[object, ...] | None:
        cursor.execute(
            """
            SELECT reservation_id, tenant_id, processing_operation_id, processing_attempt_id,
                   processing_work_item_id, document_version_id, source_artifact_id,
                   provider_name, model_policy, processing_policy_version, prompt_version,
                   canonical_schema_version, source_scope_id, request_fingerprint,
                   structural_scope_ids, source_checksum_sha256, source_size_bytes,
                   reservation_state, reservation_generation, reservation_expires_at,
                   reserved_at, in_progress_at, completed_at, provider_request_id,
                   provider_response_id, provider_result_id, validated_result, usage, latency_ms,
                   created_at, updated_at
              FROM document_ai_provider_result_reservations
             WHERE tenant_id = %s AND processing_operation_id = %s
            """,
            (lease.tenant_id, lease.processing_operation_id),
        )
        return cursor.fetchone()

    def _load_existing_result_row(
        self,
        *,
        cursor: Any,
        lease: ProcessingAttemptLease,
    ) -> tuple[object, ...] | None:
        cursor.execute(
            """
            SELECT provider_result_id
              FROM document_ai_provider_results
             WHERE tenant_id = %s AND processing_operation_id = %s
               AND provider_result_state = 'validated'
             ORDER BY created_at ASC
             LIMIT 1
            """,
            (lease.tenant_id, lease.processing_operation_id),
        )
        return cursor.fetchone()

    @staticmethod
    def _reservation_from_row(row: tuple[object, ...]) -> ProviderResultReservation:
        return ProviderResultReservation(
            reservation_id=str(row[0]),
            reservation_state=str(row[17]),
            reservation_generation=int(row[18]),
            request_fingerprint=str(row[13]),
            result_reference=None if row[25] is None else f"provider-result:{row[25]}",
            provider_request_id=None if row[23] is None else str(row[23]),
            provider_response_id=None if row[24] is None else str(row[24]),
            can_call_provider=str(row[17]) in {"reserved", "in_progress"} and row[25] is None,
        )

    @staticmethod
    def _blocked_row(row: tuple[object, ...]) -> tuple[object, ...]:
        blocked = list(row)
        blocked[17] = "blocked"
        return tuple(blocked)

    @staticmethod
    def _build_request_fingerprint(
        *, lease: ProcessingAttemptLease, details: ProviderResultReservationDetails
    ) -> str:
        envelope = {
            "tenant_id": lease.tenant_id,
            "processing_operation_id": str(lease.processing_operation_id),
            "processing_work_item_id": str(lease.processing_work_item_id),
            "processing_attempt_id": str(lease.processing_attempt_id),
            "model": details.model,
            "processing_policy_version": details.processing_policy_version,
            "prompt_version": details.prompt_version,
            "canonical_schema_version": details.canonical_schema_version,
            "document_version_id": details.document_version_id,
            "source_artifact_id": details.source_artifact_id,
            "source_scope_id": details.source_scope_id,
            "structural_scope_ids": list(details.structural_scope_ids),
            "source_checksum_sha256": details.source_checksum_sha256,
            "source_size_bytes": details.source_size_bytes,
        }
        return compute_canonical_hash(envelope).sha256_hex

    def _now(self, cursor: Any) -> Any:
        cursor.execute("SELECT now()")
        row = cursor.fetchone()
        return None if row is None else row[0]

    @staticmethod
    def _queue_canonical_assembly(
        *,
        cursor: Any,
        tenant_id: str,
        document_version_id: str,
        source_artifact_id: str,
        provider_result_id: str,
        correlation_id: str,
    ) -> None:
        """Create exactly one durable canonical-assembly intent with the result."""

        cursor.execute(
            """INSERT INTO document_ai_processing_operations (
                   tenant_id, document_version_id, operation_kind, processing_policy_version,
                   processor_version, correlation_id, request_payload
               ) VALUES (%s, %s, 'canonical_assembly', 'v1', 'canonical-assembly-v1', %s,
                         jsonb_build_object('provider_result_id', %s, 'source_artifact_id', %s))
               ON CONFLICT (tenant_id, document_version_id, operation_kind) DO NOTHING""",
            (
                tenant_id,
                document_version_id,
                correlation_id,
                provider_result_id,
                source_artifact_id,
            ),
        )
        cursor.execute(
            """INSERT INTO document_ai_processing_work_items (
                   tenant_id, processing_operation_id, work_kind, state, workload_class, priority,
                   max_attempts, max_retry_elapsed_seconds
               ) SELECT tenant_id, processing_operation_id, 'canonical_assembly', 'queued',
                        'background', 10, 3, 900
                  FROM document_ai_processing_operations
                 WHERE tenant_id = %s AND document_version_id = %s
                   AND operation_kind = 'canonical_assembly'
               ON CONFLICT (tenant_id, processing_operation_id, work_kind) DO NOTHING""",
            (tenant_id, document_version_id),
        )
        cursor.execute(
            """INSERT INTO document_ai_processing_outbox (
                   tenant_id, processing_operation_id, processing_work_item_id, event_type,
                   routing_key, correlation_id, payload
               ) SELECT operation.tenant_id, operation.processing_operation_id,
                        work.processing_work_item_id, 'canonical_assembly_requested',
                        'document_ai.processing', %s,
                        jsonb_build_object('provider_result_id', %s)
                  FROM document_ai_processing_operations AS operation
                  JOIN document_ai_processing_work_items AS work
                    ON work.tenant_id = operation.tenant_id
                   AND work.processing_operation_id = operation.processing_operation_id
                 WHERE operation.tenant_id = %s AND operation.document_version_id = %s
                   AND operation.operation_kind = 'canonical_assembly'
               ON CONFLICT (tenant_id, processing_operation_id, event_type) DO NOTHING""",
            (correlation_id, provider_result_id, tenant_id, document_version_id),
        )
