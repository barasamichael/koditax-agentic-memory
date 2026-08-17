"""Bounded CockroachDB reconciliation for durable processing state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from services.document_ai.app.config import get_document_ai_work_discovery_max_batch_size
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction
from services.document_ai.app.processing_workers import ProcessingWorkerRepository
from services.document_ai.app.retry_policy import DEFAULT_DOCUMENT_AI_RETRY_POLICY

_ELIGIBLE_DOCUMENT_STATES = ("uploaded", "processing", "validated", "active")
_RETRYABLE_REASON_CODES = tuple(DEFAULT_DOCUMENT_AI_RETRY_POLICY.retryable_reason_codes)
_RETRYABLE_REASON_SET = set(_RETRYABLE_REASON_CODES)
_RECONCILING_OPERATION_KINDS = (
    "source_inspection",
    "general_document_understanding",
    "canonical_assembly",
)


@dataclass(frozen=True)
class ProcessingStateReconciliationReport:
    """Summarize one bounded reconciliation pass."""

    expired_leases_recovered: int = 0
    retry_schedules_repaired: int = 0
    dead_letters_repaired: int = 0
    outbox_rows_repaired: int = 0
    leased_work_restored: int = 0
    cancelled_work_suppressed: int = 0

    @property
    def repaired_total(self) -> int:
        return (
            self.expired_leases_recovered
            + self.retry_schedules_repaired
            + self.dead_letters_repaired
            + self.outbox_rows_repaired
            + self.leased_work_restored
            + self.cancelled_work_suppressed
        )


class ProcessingStateReconciler:
    """Repair only deterministic, bounded processing-state inconsistencies."""

    def __init__(self, *, database_url: str, batch_size: int | None = None) -> None:
        self._database_url = database_url
        resolved_batch_size = (
            get_document_ai_work_discovery_max_batch_size()
            if batch_size is None
            else batch_size
        )
        if resolved_batch_size < 1:
            raise ValueError("document_ai_processing_state_reconciliation_batch_size_invalid")
        self._batch_size = resolved_batch_size
        self._worker_repository = ProcessingWorkerRepository(database_url=database_url)

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def reconcile_once(self) -> ProcessingStateReconciliationReport:
        """Run one bounded reconciliation pass across the durable processing graph."""

        expired_leases = self._worker_repository.recover_expired_leases(limit=self._batch_size)
        remaining = max(0, self._batch_size - expired_leases)
        retry_schedules = self._repair_missing_retry_schedules(limit=remaining)
        remaining = max(0, remaining - retry_schedules)
        dead_letters = self._repair_dead_letters(limit=remaining)
        remaining = max(0, remaining - dead_letters)
        outbox_rows = self._repair_missing_outbox_rows(limit=remaining)
        remaining = max(0, remaining - outbox_rows)
        leased_work = self._restore_running_leases(limit=remaining)
        remaining = max(0, remaining - leased_work)
        cancelled_work = self._suppress_cancelled_work(limit=remaining)

        return ProcessingStateReconciliationReport(
            expired_leases_recovered=expired_leases,
            retry_schedules_repaired=retry_schedules,
            dead_letters_repaired=dead_letters,
            outbox_rows_repaired=outbox_rows,
            leased_work_restored=leased_work,
            cancelled_work_suppressed=cancelled_work,
        )

    def _repair_missing_retry_schedules(self, *, limit: int) -> int:
        if limit < 1:
            return 0

        def _repair(cursor: Any) -> int:
            cursor.execute(
                """
                SELECT work.tenant_id, work.processing_work_item_id,
                       work.processing_operation_id, work.retry_count,
                       work.max_attempts, work.first_attempted_at,
                       work.max_retry_elapsed_seconds, work.failure_category,
                       attempt.processing_attempt_id, attempt.attempt_number,
                       attempt.error_detail, attempt.started_at,
                       operation.correlation_id, operation.document_version_id,
                       operation.operation_kind, version.document_id,
                       artifact.source_artifact_id
                FROM document_ai_processing_work_items AS work
                JOIN document_ai_processing_operations AS operation
                  ON operation.tenant_id = work.tenant_id
                 AND operation.processing_operation_id = work.processing_operation_id
                JOIN document_ai_document_versions AS version
                  ON version.tenant_id = operation.tenant_id
                 AND version.document_version_id = operation.document_version_id
                JOIN document_ai_documents AS document
                  ON document.tenant_id = version.tenant_id
                 AND document.document_id = version.document_id
                JOIN document_ai_source_artifacts AS artifact
                  ON artifact.tenant_id = version.tenant_id
                 AND artifact.document_version_id = version.document_version_id
                JOIN LATERAL (
                    SELECT attempt.processing_attempt_id, attempt.attempt_number,
                           attempt.error_detail, attempt.started_at, attempt.state
                    FROM document_ai_processing_attempts AS attempt
                    WHERE attempt.tenant_id = work.tenant_id
                      AND attempt.processing_work_item_id = work.processing_work_item_id
                    ORDER BY attempt.attempt_number DESC, attempt.started_at DESC
                    LIMIT 1
                ) AS attempt ON true
                WHERE work.state = 'failed'
                  AND work.next_retry_at IS NULL
                  AND work.dead_lettered_at IS NULL
                  AND work.failure_category = ANY(%s)
                  AND work.retry_count < work.max_attempts
                  AND attempt.state = 'failed'
                  AND operation.state IN ('queued', 'running')
                  AND operation.cancellation_requested_at IS NULL
                  AND version.version_state = 'current'
                  AND document.state = ANY(%s)
                ORDER BY work.available_at ASC, work.priority DESC, work.created_at ASC,
                         work.processing_work_item_id ASC
                LIMIT %s
                FOR UPDATE OF work SKIP LOCKED
                """,
                (list(_RETRYABLE_REASON_CODES), list(_ELIGIBLE_DOCUMENT_STATES), limit),
            )
            rows = cursor.fetchall()
            repaired = 0
            for row in rows:
                retry_count = int(row[3])
                attempt_number = int(row[9])
                retry_after_ms = _extract_retry_after_ms(row[10])
                delay_ms = DEFAULT_DOCUMENT_AI_RETRY_POLICY.scheduled_delay_ms(
                    attempt_count=attempt_number,
                    retry_after_ms=retry_after_ms,
                    jitter=0.0,
                )
                delay_interval = f"{delay_ms / 1000} seconds"
                cursor.execute(
                    """
                    UPDATE document_ai_processing_work_items
                    SET state = 'queued',
                        retry_count = GREATEST(retry_count, %s),
                        first_attempted_at = COALESCE(first_attempted_at, %s),
                        available_at = now() + %s::interval,
                        next_retry_at = now() + %s::interval,
                        failure_category = %s,
                        leased_until = NULL,
                        lease_issued_at = NULL,
                        last_heartbeat_at = NULL,
                        current_processing_attempt_id = NULL
                    WHERE tenant_id = %s
                      AND processing_work_item_id = %s
                      AND state = 'failed'
                      AND next_retry_at IS NULL
                      AND dead_lettered_at IS NULL
                    """,
                    (
                        max(retry_count, attempt_number),
                        row[11],
                        delay_interval,
                        delay_interval,
                        row[7],
                        row[0],
                        row[1],
                    ),
                )
                cursor.execute(
                    """
                    UPDATE document_ai_processing_operations
                    SET state = 'queued', completed_at = NULL, failure_category = NULL
                    WHERE tenant_id = %s
                      AND processing_operation_id = %s
                      AND state IN ('queued', 'running')
                      AND cancellation_requested_at IS NULL
                    """,
                    (row[0], row[2]),
                )
                cursor.execute(
                    """
                    INSERT INTO document_ai_processing_outbox (
                        tenant_id, processing_operation_id, processing_work_item_id,
                        event_type, payload, routing_key, correlation_id
                    ) VALUES (%s, %s, %s, %s, '{}'::jsonb, 'document_ai.processing', %s)
                    ON CONFLICT (tenant_id, processing_operation_id, event_type) DO NOTHING
                    """,
                    (
                        row[0],
                        row[2],
                        row[1],
                        f"processing.retry.{row[8]}",
                        row[12],
                    ),
                )
                repaired += 1
            return repaired

        def _reconcile(connection: Any) -> int | None:
            with connection.cursor() as cursor:
                cursor.execute(
                """
                SELECT COUNT(*)
                FROM document_ai_processing_work_items AS work
                JOIN document_ai_processing_operations AS operation
                  ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                WHERE work.state = 'queued'
                      AND work.next_retry_at IS NOT NULL
                      AND work.dead_lettered_at IS NULL
                      AND work.failure_category = ANY(%s)
                      AND operation.state = 'queued'
                      AND operation.cancellation_requested_at IS NULL
                    """,
                    (list(_RETRYABLE_REASON_CODES),),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return int(row[0])

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.processing_reconciliation.retry_schedule",
            transaction_callback=_repair,
            reconcile_ambiguous_result=_reconcile,
        )

    def _repair_dead_letters(self, *, limit: int) -> int:
        if limit < 1:
            return 0

        def _repair(cursor: Any) -> int:
            cursor.execute(
                """
                SELECT work.tenant_id, work.processing_work_item_id,
                       work.processing_operation_id, work.work_kind, work.failure_category,
                       work.dead_letter_reason, work.retry_count, work.max_attempts,
                       work.max_retry_elapsed_seconds, work.dead_lettered_at,
                       work.fencing_token, attempt.processing_attempt_id, attempt.attempt_number,
                       attempt.error_code, attempt.error_detail, attempt.started_at,
                       operation.correlation_id, operation.document_version_id,
                       operation.operation_kind, version.document_id,
                       artifact.source_artifact_id
                FROM document_ai_processing_work_items AS work
                JOIN document_ai_processing_operations AS operation
                  ON operation.tenant_id = work.tenant_id
                 AND operation.processing_operation_id = work.processing_operation_id
                JOIN document_ai_document_versions AS version
                  ON version.tenant_id = operation.tenant_id
                 AND version.document_version_id = operation.document_version_id
                JOIN document_ai_documents AS document
                  ON document.tenant_id = version.tenant_id
                 AND document.document_id = version.document_id
                JOIN document_ai_source_artifacts AS artifact
                  ON artifact.tenant_id = version.tenant_id
                 AND artifact.document_version_id = version.document_version_id
                JOIN LATERAL (
                    SELECT attempt.processing_attempt_id, attempt.attempt_number,
                           attempt.error_code, attempt.error_detail, attempt.started_at,
                           attempt.state
                    FROM document_ai_processing_attempts AS attempt
                    WHERE attempt.tenant_id = work.tenant_id
                      AND attempt.processing_work_item_id = work.processing_work_item_id
                    ORDER BY attempt.attempt_number DESC, attempt.started_at DESC
                    LIMIT 1
                ) AS attempt ON true
                WHERE work.state = 'dead_letter'
                  AND work.dead_lettered_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM document_ai_processing_dead_letters AS dead_letter
                      WHERE dead_letter.tenant_id = work.tenant_id
                        AND dead_letter.processing_attempt_id = attempt.processing_attempt_id
                  )
                  AND operation.state IN ('queued', 'running', 'failed')
                  AND operation.cancellation_requested_at IS NULL
                  AND version.version_state = 'current'
                  AND document.state = ANY(%s)
                ORDER BY work.dead_lettered_at ASC, work.created_at ASC,
                         work.processing_work_item_id ASC
                LIMIT %s
                FOR UPDATE OF work SKIP LOCKED
                """,
                (list(_ELIGIBLE_DOCUMENT_STATES), limit),
            )
            rows = cursor.fetchall()
            repaired = 0
            for row in rows:
                failure_category = str(row[4]) if row[4] is not None else ""
                dead_letter_reason = (
                    str(row[5]) if row[5] is not None else _dead_letter_reason(failure_category)
                )
                failure_class = (
                    "retry_exhausted"
                    if failure_category in _RETRYABLE_REASON_SET
                    else "non_retryable_failure"
                )
                error_detail = _normalize_json_mapping(row[14])
                diagnostic_payload = {
                    "attempt_number": int(row[12]),
                    "correlation_id": str(row[16]),
                    "error_code": str(row[13]) if row[13] is not None else dead_letter_reason,
                    "error_detail": error_detail or {},
                    "failure_class": failure_class,
                    "failure_category": failure_category,
                    "reason_code": dead_letter_reason,
                    "retry_count": int(row[6]),
                    "retryable": failure_category in _RETRYABLE_REASON_SET,
                }
                cursor.execute(
                    """
                    INSERT INTO document_ai_processing_dead_letters (
                        tenant_id, processing_operation_id, processing_work_item_id,
                        processing_attempt_id, attempt_number, document_id, document_version_id,
                        source_artifact_id, work_kind, operation_kind, worker_id, fencing_token,
                        failure_class, failure_category, reason_code, retry_count, max_attempts,
                        max_retry_elapsed_seconds, correlation_id, error_code, error_detail,
                        dead_lettered_at, created_at, diagnostic_payload
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s::jsonb, now(), now(), %s::jsonb
                    )
                    ON CONFLICT (tenant_id, processing_attempt_id) DO NOTHING
                    """,
                    (
                        row[0],
                        row[2],
                        row[1],
                        row[11],
                        int(row[12]),
                        row[19],
                        row[17],
                        row[20],
                        row[3],
                        row[18],
                        "reconciler",
                        int(row[10]) if row[10] is not None else 0,
                        failure_class,
                        failure_category,
                        dead_letter_reason,
                        int(row[6]),
                        int(row[7]),
                        int(row[8]),
                        row[16],
                        str(row[13]) if row[13] is not None else dead_letter_reason,
                        json.dumps(error_detail or {}, sort_keys=True),
                        json.dumps(diagnostic_payload, sort_keys=True),
                    ),
                )
                cursor.execute(
                    """
                    UPDATE document_ai_processing_operations
                    SET state = 'failed', completed_at = COALESCE(completed_at, now()),
                        failure_category = COALESCE(failure_category, %s)
                    WHERE tenant_id = %s
                      AND processing_operation_id = %s
                      AND state IN ('queued', 'running', 'failed')
                    """,
                    (dead_letter_reason, row[0], row[2]),
                )
                repaired += 1
            return repaired

        def _reconcile(connection: Any) -> int | None:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_ai_processing_dead_letters AS dead_letter
                    WHERE dead_letter.failure_class IN ('retry_exhausted', 'non_retryable_failure')
                    """,
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return int(row[0])

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.processing_reconciliation.dead_letters",
            transaction_callback=_repair,
            reconcile_ambiguous_result=_reconcile,
        )

    def _repair_missing_outbox_rows(self, *, limit: int) -> int:
        if limit < 1:
            return 0

        def _repair(cursor: Any) -> int:
            cursor.execute(
                """
                SELECT work.tenant_id, work.processing_work_item_id,
                       work.processing_operation_id, operation.operation_kind,
                       operation.correlation_id, operation.document_version_id,
                       version.document_id, provider_result.provider_result_id
                FROM document_ai_processing_work_items AS work
                JOIN document_ai_processing_operations AS operation
                  ON operation.tenant_id = work.tenant_id
                 AND operation.processing_operation_id = work.processing_operation_id
                JOIN document_ai_document_versions AS version
                  ON version.tenant_id = operation.tenant_id
                 AND version.document_version_id = operation.document_version_id
                JOIN document_ai_documents AS document
                  ON document.tenant_id = version.tenant_id
                 AND document.document_id = version.document_id
                LEFT JOIN document_ai_provider_results AS provider_result
                  ON provider_result.tenant_id = operation.tenant_id
                 AND provider_result.processing_operation_id = operation.processing_operation_id
                WHERE work.state = 'queued'
                  AND work.available_at <= now()
                  AND work.leased_until IS NULL
                  AND work.current_processing_attempt_id IS NULL
                  AND work.dead_lettered_at IS NULL
                  AND operation.state IN ('queued', 'running')
                  AND operation.cancellation_requested_at IS NULL
                  AND version.version_state = 'current'
                  AND document.state = ANY(%s)
                  AND operation.operation_kind = ANY(%s)
                  AND (
                      (operation.operation_kind = 'source_inspection' AND NOT EXISTS (
                          SELECT 1
                          FROM document_ai_processing_outbox AS outbox
                          WHERE outbox.tenant_id = work.tenant_id
                            AND outbox.processing_operation_id = work.processing_operation_id
                            AND outbox.event_type = 'source_inspection_requested'
                      ))
                      OR (operation.operation_kind = 'general_document_understanding' AND NOT EXISTS (
                          SELECT 1
                          FROM document_ai_processing_outbox AS outbox
                          WHERE outbox.tenant_id = work.tenant_id
                            AND outbox.processing_operation_id = work.processing_operation_id
                            AND outbox.event_type = 'general_document_understanding_requested'
                      ))
                      OR (operation.operation_kind = 'canonical_assembly'
                          AND provider_result.provider_result_id IS NOT NULL
                          AND NOT EXISTS (
                              SELECT 1
                              FROM document_ai_processing_outbox AS outbox
                              WHERE outbox.tenant_id = work.tenant_id
                                AND outbox.processing_operation_id = work.processing_operation_id
                                AND outbox.event_type = 'canonical_assembly_requested'
                          )
                      )
                  )
                ORDER BY work.available_at ASC, work.priority DESC, work.created_at ASC,
                         work.processing_work_item_id ASC
                LIMIT %s
                FOR UPDATE OF work SKIP LOCKED
                """,
                (list(_ELIGIBLE_DOCUMENT_STATES), list(_RECONCILING_OPERATION_KINDS), limit),
            )
            rows = cursor.fetchall()
            repaired = 0
            for row in rows:
                operation_kind = str(row[3])
                if operation_kind == "source_inspection":
                    event_type = "source_inspection_requested"
                    payload: dict[str, object] = {
                        "document_id": str(row[6]),
                        "version_id": str(row[5]),
                    }
                elif operation_kind == "general_document_understanding":
                    event_type = "general_document_understanding_requested"
                    payload = {"version_id": str(row[5])}
                elif operation_kind == "canonical_assembly":
                    if row[7] is None:
                        continue
                    event_type = "canonical_assembly_requested"
                    payload = {"provider_result_id": str(row[7])}
                else:
                    continue
                cursor.execute(
                    """
                    INSERT INTO document_ai_processing_outbox (
                        tenant_id, processing_operation_id, processing_work_item_id,
                        event_type, payload, routing_key, correlation_id
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, 'document_ai.processing', %s)
                    ON CONFLICT (tenant_id, processing_operation_id, event_type) DO NOTHING
                    """,
                    (
                        row[0],
                        row[2],
                        row[1],
                        event_type,
                        json.dumps(payload, sort_keys=True),
                        row[4],
                    ),
                )
                repaired += 1
            return repaired

        def _reconcile(connection: Any) -> int | None:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_ai_processing_outbox AS outbox
                    WHERE outbox.event_type IN (
                        'source_inspection_requested',
                        'general_document_understanding_requested',
                        'canonical_assembly_requested'
                    )
                    """,
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return int(row[0])

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.processing_reconciliation.outbox",
            transaction_callback=_repair,
            reconcile_ambiguous_result=_reconcile,
        )

    def _restore_running_leases(self, *, limit: int) -> int:
        if limit < 1:
            return 0

        def _repair(cursor: Any) -> int:
            cursor.execute(
                """
                SELECT work.tenant_id, work.processing_work_item_id,
                       work.processing_operation_id, work.current_processing_attempt_id,
                       work.fencing_token, work.state, work.leased_until,
                       attempt.processing_attempt_id, attempt.worker_id, attempt.fencing_token,
                       attempt.lease_expires_at, attempt.last_heartbeat_at, attempt.started_at
                FROM document_ai_processing_work_items AS work
                JOIN document_ai_processing_attempts AS attempt
                  ON attempt.tenant_id = work.tenant_id
                 AND attempt.processing_attempt_id = work.current_processing_attempt_id
                JOIN document_ai_processing_operations AS operation
                  ON operation.tenant_id = work.tenant_id
                 AND operation.processing_operation_id = work.processing_operation_id
                JOIN document_ai_document_versions AS version
                  ON version.tenant_id = operation.tenant_id
                 AND version.document_version_id = operation.document_version_id
                JOIN document_ai_documents AS document
                  ON document.tenant_id = version.tenant_id
                 AND document.document_id = version.document_id
                WHERE work.current_processing_attempt_id IS NOT NULL
                  AND work.state IN ('queued', 'failed')
                  AND attempt.state = 'running'
                  AND attempt.lease_expires_at IS NOT NULL
                  AND operation.state IN ('queued', 'running')
                  AND operation.cancellation_requested_at IS NULL
                  AND version.version_state = 'current'
                  AND document.state = ANY(%s)
                ORDER BY work.created_at ASC, work.processing_work_item_id ASC
                LIMIT %s
                FOR UPDATE OF work, attempt SKIP LOCKED
                """,
                (list(_ELIGIBLE_DOCUMENT_STATES), limit),
            )
            rows = cursor.fetchall()
            repaired = 0
            for row in rows:
                cursor.execute(
                    """
                    UPDATE document_ai_processing_work_items
                    SET state = 'leased',
                        fencing_token = COALESCE(%s, fencing_token),
                        lease_issued_at = COALESCE(lease_issued_at, %s),
                        last_heartbeat_at = COALESCE(%s, now()),
                        leased_until = COALESCE(%s, leased_until)
                    WHERE tenant_id = %s
                      AND processing_work_item_id = %s
                      AND current_processing_attempt_id = %s
                    """,
                    (
                        row[9],
                        row[12],
                        row[11],
                        row[10],
                        row[0],
                        row[1],
                        row[3],
                    ),
                )
                repaired += 1
            return repaired

        def _reconcile(connection: Any) -> int | None:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_ai_processing_work_items AS work
                    JOIN document_ai_processing_attempts AS attempt
                      ON attempt.tenant_id = work.tenant_id
                     AND attempt.processing_attempt_id = work.current_processing_attempt_id
                    WHERE work.state = 'leased'
                      AND attempt.state = 'running'
                      AND attempt.lease_expires_at IS NOT NULL
                    """,
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return int(row[0])

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.processing_reconciliation.restore_leases",
            transaction_callback=_repair,
            reconcile_ambiguous_result=_reconcile,
        )

    def _suppress_cancelled_work(self, *, limit: int) -> int:
        if limit < 1:
            return 0

        def _repair(cursor: Any) -> int:
            cursor.execute(
                """
                SELECT work.tenant_id, work.processing_work_item_id
                FROM document_ai_processing_work_items AS work
                JOIN document_ai_processing_operations AS operation
                  ON operation.tenant_id = work.tenant_id
                 AND operation.processing_operation_id = work.processing_operation_id
                JOIN document_ai_document_versions AS version
                  ON version.tenant_id = operation.tenant_id
                 AND version.document_version_id = operation.document_version_id
                JOIN document_ai_documents AS document
                  ON document.tenant_id = version.tenant_id
                 AND document.document_id = version.document_id
                WHERE work.state IN ('queued', 'leased')
                  AND work.current_processing_attempt_id IS NULL
                  AND operation.state = 'cancelled'
                  AND version.version_state = 'current'
                  AND document.state = ANY(%s)
                ORDER BY work.created_at ASC, work.processing_work_item_id ASC
                LIMIT %s
                FOR UPDATE OF work SKIP LOCKED
                """,
                (list(_ELIGIBLE_DOCUMENT_STATES), limit),
            )
            rows = cursor.fetchall()
            repaired = 0
            for row in rows:
                cursor.execute(
                    """
                    UPDATE document_ai_processing_work_items
                    SET state = 'cancelled',
                        leased_until = NULL,
                        lease_issued_at = NULL,
                        last_heartbeat_at = NULL,
                        current_processing_attempt_id = NULL
                    WHERE tenant_id = %s
                      AND processing_work_item_id = %s
                      AND state IN ('queued', 'leased')
                    """,
                    (row[0], row[1]),
                )
                repaired += 1
            return repaired

        def _reconcile(connection: Any) -> int | None:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM document_ai_processing_work_items AS work
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                    WHERE work.state = 'cancelled'
                      AND operation.state = 'cancelled'
                    """,
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return int(row[0])

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.processing_reconciliation.cancelled_work",
            transaction_callback=_repair,
            reconcile_ambiguous_result=_reconcile,
        )


def _normalize_json_mapping(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return dict(parsed)
        return None
    try:
        mapping = dict(value)  # type: ignore[arg-type]
    except Exception:
        return None
    return dict(mapping)


def _extract_retry_after_ms(value: object) -> int | None:
    mapping = _normalize_json_mapping(value)
    if mapping is None:
        return None
    retry_after_ms = mapping.get("retry_after_ms")
    if isinstance(retry_after_ms, int) and retry_after_ms > 0:
        return retry_after_ms
    return None


def _dead_letter_reason(failure_category: str) -> str:
    if failure_category in _RETRYABLE_REASON_SET:
        return "retry_budget_exhausted"
    return failure_category or "processing_failure"
