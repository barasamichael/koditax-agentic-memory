"""Durable worker claims, leases, checkpoints, and fenced result commits.

This module is deliberately provider-neutral.  A queue delivery is only a
wake-up signal: every decision is made from PostgreSQL processing state.
"""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from typing import Protocol
from datetime import datetime
from dataclasses import dataclass

from psycopg import sql

from services.document_ai.app.outbox import ProcessingWorkMessage
from services.document_ai.app.metrics import get_default_metrics_emitter
from services.document_ai.app.metrics import DOCUMENT_PROCESSING_RETRIES_TOTAL
from services.document_ai.app.metrics import DOCUMENT_PROCESSING_DEAD_LETTERS_TOTAL
from services.document_ai.app.retry_policy import RetryPolicyConfig
from services.document_ai.app.retry_policy import RetryClassifiedFailure
from services.document_ai.app.retry_policy import classify_document_ai_failure
from services.document_ai.app.retry_policy import DEFAULT_DOCUMENT_AI_RETRY_POLICY
from services.document_ai.app.config import get_document_ai_work_discovery_max_batch_size
from services.document_ai.app.logging_context import emit_document_structured_log
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction
from services.document_ai.app.processing_work_discovery import ProcessingWorkCandidate

_ELIGIBLE_DOCUMENT_STATES = ("uploaded", "processing", "validated", "active")
_LEASE_EXPIRED_FAILURE_CODE = "lease_expired"


@dataclass(frozen=True)
class ProcessingFailureDisposition:
    """Durable consequence of a fenced failed processing attempt."""

    state: str
    retry_scheduled: bool


@dataclass(frozen=True)
class ProcessingAttemptLease:
    """The sole credential permitted to mutate a claimed work item."""

    tenant_id: str
    processing_operation_id: UUID
    processing_work_item_id: UUID
    processing_attempt_id: UUID
    worker_id: str
    fencing_token: int
    lease_seconds: int
    correlation_id: str


@dataclass(frozen=True)
class ProcessingWorkClaimRecord:
    """Return the exact durable ownership state established by a successful claim."""

    tenant_id: str
    processing_operation_id: UUID
    processing_work_item_id: UUID
    document_id: UUID
    document_version_id: UUID
    source_artifact_id: UUID
    work_kind: str
    operation_kind: str
    work_state: str
    priority: int
    available_at: datetime
    created_at: datetime
    retry_count: int
    max_attempts: int
    next_retry_at: datetime | None
    failure_category: str | None
    processing_attempt_id: UUID
    worker_id: str
    fencing_token: int
    lease_seconds: int
    correlation_id: str

    def to_lease(self) -> ProcessingAttemptLease:
        """Project the claim record into the stable worker lease envelope."""

        return ProcessingAttemptLease(
            tenant_id=self.tenant_id,
            processing_operation_id=self.processing_operation_id,
            processing_work_item_id=self.processing_work_item_id,
            processing_attempt_id=self.processing_attempt_id,
            worker_id=self.worker_id,
            fencing_token=self.fencing_token,
            lease_seconds=self.lease_seconds,
            correlation_id=self.correlation_id,
        )


@dataclass(frozen=True)
class ProcessingDeadLetterRecord:
    """Represent the durable CockroachDB dead-letter authority record."""

    processing_dead_letter_id: UUID
    tenant_id: str
    processing_operation_id: UUID
    processing_work_item_id: UUID
    processing_attempt_id: UUID
    attempt_number: int
    document_id: UUID
    document_version_id: UUID
    source_artifact_id: UUID
    work_kind: str
    operation_kind: str
    worker_id: str
    fencing_token: int
    failure_class: str
    failure_category: str
    reason_code: str
    retry_count: int
    max_attempts: int
    max_retry_elapsed_seconds: int
    correlation_id: str
    error_code: str
    error_detail: dict[str, object]
    dead_lettered_at: datetime
    created_at: datetime
    diagnostic_payload: dict[str, object]


@dataclass(frozen=True)
class DurableCheckpoint:
    """A monotonically advancing, provider-safe progress record."""

    checkpoint_key: str
    sequence: int
    payload: dict[str, object]


class ProcessingWorkExecutor(Protocol):
    """Worker implementation; it must not commit outside its supplied lease."""

    def execute(
        self, *, lease: ProcessingAttemptLease, checkpoint: DurableCheckpoint | None
    ) -> str: ...


class QueueDeliveryProtocol(Protocol):
    """Minimal queue boundary used to keep acknowledgement after durable commit."""

    message: ProcessingWorkMessage

    def acknowledge(self) -> None: ...


@dataclass
class ProcessingWorkCandidateClaimHandoff:
    """Translate discovered candidates into conditional durable claims."""

    repository: ProcessingWorkerRepository
    worker_id: str

    def handoff(self, *, candidate: ProcessingWorkCandidate) -> bool:
        """Attempt to claim the candidate and report whether ownership was won."""

        return (
            self.repository.claim_candidate(candidate=candidate, worker_id=self.worker_id)
            is not None
        )


class ProcessingWorkerRepository:
    """PostgreSQL authority for the Milestone 11 worker execution boundary."""

    def __init__(self, *, database_url: str, lease_seconds: int = 60) -> None:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("processing_worker_lease_seconds_out_of_range")
        self._database_url = database_url
        self._lease_seconds = lease_seconds

    def _load_failure_replay_row(
        self,
        *,
        tenant_id: str,
        processing_work_item_id: UUID,
        processing_attempt_id: UUID,
    ) -> tuple[object, ...] | None:
        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT work.state, work.failure_category, work.dead_letter_reason,
                           work.dead_lettered_at, work.retry_count,
                           attempt.state, attempt.error_code, attempt.error_detail,
                           operation.state, operation.completed_at, operation.failure_category
                    FROM document_ai_processing_work_items AS work
                    JOIN document_ai_processing_attempts AS attempt
                      ON attempt.tenant_id = work.tenant_id
                     AND attempt.processing_attempt_id = %s
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                    WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                    """,
                    (processing_attempt_id, tenant_id, processing_work_item_id),
                )
                return cursor.fetchone()

    def _load_dead_letter_record(
        self,
        *,
        tenant_id: str,
        processing_attempt_id: UUID,
    ) -> ProcessingDeadLetterRecord | None:
        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT processing_dead_letter_id, tenant_id, processing_operation_id,
                           processing_work_item_id, processing_attempt_id, attempt_number,
                           document_id, document_version_id, source_artifact_id, work_kind,
                           operation_kind, worker_id, fencing_token, failure_class,
                           failure_category, reason_code, retry_count, max_attempts,
                           max_retry_elapsed_seconds, correlation_id, error_code,
                           error_detail, dead_lettered_at, created_at, diagnostic_payload
                    FROM document_ai_processing_dead_letters
                    WHERE tenant_id = %s AND processing_attempt_id = %s
                    """,
                    (tenant_id, processing_attempt_id),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return ProcessingDeadLetterRecord(
            processing_dead_letter_id=UUID(str(row[0])),
            tenant_id=str(row[1]),
            processing_operation_id=UUID(str(row[2])),
            processing_work_item_id=UUID(str(row[3])),
            processing_attempt_id=UUID(str(row[4])),
            attempt_number=int(row[5]),
            document_id=UUID(str(row[6])),
            document_version_id=UUID(str(row[7])),
            source_artifact_id=UUID(str(row[8])),
            work_kind=str(row[9]),
            operation_kind=str(row[10]),
            worker_id=str(row[11]),
            fencing_token=int(row[12]),
            failure_class=str(row[13]),
            failure_category=str(row[14]),
            reason_code=str(row[15]),
            retry_count=int(row[16]),
            max_attempts=int(row[17]),
            max_retry_elapsed_seconds=int(row[18]),
            correlation_id=str(row[19]),
            error_code=str(row[20]),
            error_detail=_normalize_json_mapping(row[21]) or {},
            dead_lettered_at=row[22],
            created_at=row[23],
            diagnostic_payload=_normalize_json_mapping(row[24]) or {},
        )

    def recover_expired_leases(self, *, limit: int | None = None) -> int:
        """Make expired work recoverable and retain the expired attempt as audit history."""
        resolved_limit = (
            get_document_ai_work_discovery_max_batch_size() if limit is None else limit
        )
        if resolved_limit < 1:
            return 0

        def _recover(cursor) -> int:  # type: ignore[no-untyped-def]
            cursor.execute(
                """
                SELECT work.tenant_id, work.processing_work_item_id,
                       work.processing_operation_id, work.retry_count,
                       work.max_attempts, work.first_attempted_at,
                       work.max_retry_elapsed_seconds, attempt.processing_attempt_id,
                       attempt.attempt_number
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
                WHERE work.state = 'leased'
                  AND work.leased_until <= now()
                  AND work.current_processing_attempt_id IS NOT NULL
                  AND attempt.state = 'running'
                  AND operation.cancellation_requested_at IS NULL
                  AND version.version_state = 'current'
                  AND document.state = ANY(%s)
                ORDER BY work.leased_until ASC, work.created_at ASC,
                         work.processing_work_item_id ASC
                LIMIT %s
                FOR UPDATE OF work, attempt
                """,
                (list(_ELIGIBLE_DOCUMENT_STATES), resolved_limit),
            )
            rows = cursor.fetchall()
            recovered = 0
            for row in rows:
                (
                    tenant_id,
                    processing_work_item_id,
                    processing_operation_id,
                    retry_count,
                    max_attempts,
                    first_attempted_at,
                    max_retry_elapsed_seconds,
                    processing_attempt_id,
                    attempt_number,
                ) = row
                elapsed_ok = first_attempted_at is None
                if first_attempted_at is not None:
                    cursor.execute(
                        "SELECT now() - %s <= %s::interval",
                        (
                            first_attempted_at,
                            f"{int(max_retry_elapsed_seconds)} seconds",
                        ),
                    )
                    elapsed_row = cursor.fetchone()
                    elapsed_ok = elapsed_row is not None and bool(elapsed_row[0])

                cursor.execute(
                    """
                    UPDATE document_ai_processing_attempts
                    SET state = 'failed',
                        finished_at = now(),
                        error_code = %s,
                        error_detail = %s::jsonb,
                        lease_expires_at = NULL,
                        last_heartbeat_at = NULL
                    WHERE tenant_id = %s
                      AND processing_attempt_id = %s
                      AND state = 'running'
                    """,
                    (
                        _LEASE_EXPIRED_FAILURE_CODE,
                        json.dumps({"reason": _LEASE_EXPIRED_FAILURE_CODE}, sort_keys=True),
                        tenant_id,
                        processing_attempt_id,
                    ),
                )

                if int(attempt_number) < int(max_attempts) and elapsed_ok:
                    delay_ms = DEFAULT_DOCUMENT_AI_RETRY_POLICY.scheduled_delay_ms(
                        attempt_count=int(attempt_number),
                        jitter=0.0,
                    )
                    delay_interval = f"{delay_ms / 1000} seconds"
                    cursor.execute(
                        """
                        UPDATE document_ai_processing_work_items
                        SET state = 'queued',
                            retry_count = retry_count + 1,
                            available_at = now() + %s::interval,
                            next_retry_at = now() + %s::interval,
                            failure_category = %s,
                            leased_until = NULL,
                            lease_issued_at = NULL,
                            last_heartbeat_at = NULL,
                            current_processing_attempt_id = NULL
                        WHERE tenant_id = %s
                          AND processing_work_item_id = %s
                          AND state = 'leased'
                          AND current_processing_attempt_id = %s
                        """,
                        (
                            delay_interval,
                            delay_interval,
                            _LEASE_EXPIRED_FAILURE_CODE,
                            tenant_id,
                            processing_work_item_id,
                            processing_attempt_id,
                        ),
                    )
                    cursor.execute(
                        """
                        UPDATE document_ai_processing_operations
                        SET state = 'queued', completed_at = NULL, failure_category = NULL
                        WHERE tenant_id = %s
                          AND processing_operation_id = %s
                        """,
                        (tenant_id, processing_operation_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE document_ai_processing_work_items
                        SET state = 'failed',
                            failure_category = %s,
                            leased_until = NULL,
                            lease_issued_at = NULL,
                            last_heartbeat_at = NULL,
                            current_processing_attempt_id = NULL
                        WHERE tenant_id = %s
                          AND processing_work_item_id = %s
                          AND state = 'leased'
                          AND current_processing_attempt_id = %s
                        """,
                        (
                            _LEASE_EXPIRED_FAILURE_CODE,
                            tenant_id,
                            processing_work_item_id,
                            processing_attempt_id,
                        ),
                    )
                    cursor.execute(
                        """
                        UPDATE document_ai_processing_operations
                        SET state = 'failed', completed_at = now(),
                            failure_category = %s
                        WHERE tenant_id = %s
                          AND processing_operation_id = %s
                        """,
                        (
                            "retry_budget_exhausted"
                            if int(attempt_number) >= int(max_attempts)
                            else _LEASE_EXPIRED_FAILURE_CODE,
                            tenant_id,
                            processing_operation_id,
                        ),
                    )
                recovered += 1
            return recovered

        def _reconcile(connection) -> int | None:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
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
                    WHERE work.state IN ('queued', 'failed')
                      AND work.failure_category = %s
                      AND work.leased_until IS NULL
                      AND work.current_processing_attempt_id IS NULL
                      AND operation.cancellation_requested_at IS NULL
                      AND version.version_state = 'current'
                      AND document.state = ANY(%s)
                    LIMIT %s
                    """,
                    (
                        _LEASE_EXPIRED_FAILURE_CODE,
                        list(_ELIGIBLE_DOCUMENT_STATES),
                        resolved_limit,
                    ),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return int(row[0])

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.processing_worker.recover_expired_leases",
            transaction_callback=_recover,
            reconcile_ambiguous_result=_reconcile,
        )

    def claim(
        self, *, message: ProcessingWorkMessage, worker_id: str
    ) -> ProcessingAttemptLease | None:
        """Atomically claim one eligible item and issue a newer fencing token."""

        record = self.claim_work_item(
            tenant_id=message.tenant_id,
            processing_work_item_id=message.processing_work_item_id,
            processing_operation_id=message.processing_operation_id,
            worker_id=worker_id,
        )
        return None if record is None else record.to_lease()

    def claim_candidate(
        self, *, candidate: ProcessingWorkCandidate, worker_id: str
    ) -> ProcessingWorkClaimRecord | None:
        """Claim one discovered candidate without trusting the earlier snapshot."""

        return self.claim_work_item(
            tenant_id=candidate.tenant_id,
            processing_work_item_id=candidate.processing_work_item_id,
            processing_operation_id=candidate.processing_operation_id,
            worker_id=worker_id,
        )

    def claim_work_item(
        self,
        *,
        tenant_id: str,
        processing_work_item_id: UUID,
        processing_operation_id: UUID,
        worker_id: str,
    ) -> ProcessingWorkClaimRecord | None:
        """Claim one stable work identity and return the fenced durable state."""

        if not worker_id.strip():
            raise ValueError("processing_worker_id_required")
        attempt_id = uuid4()
        interval = f"{self._lease_seconds} seconds"

        def _claim(cursor) -> ProcessingWorkClaimRecord | None:  # type: ignore[no-untyped-def]
            cursor.execute(
                """
                SELECT work.tenant_id, work.processing_work_item_id,
                       work.processing_operation_id, version.document_id,
                       operation.document_version_id, artifact.source_artifact_id,
                       work.work_kind, operation.operation_kind, work.state,
                       work.priority, work.available_at, work.created_at,
                       work.retry_count, work.max_attempts, work.next_retry_at,
                       work.failure_category, work.fencing_token,
                       work.fencing_token + 1 AS next_fence,
                       COALESCE(
                           (
                               SELECT MAX(previous.attempt_number) + 1
                               FROM document_ai_processing_attempts AS previous
                               WHERE previous.tenant_id = work.tenant_id
                                 AND previous.processing_work_item_id =
                                     work.processing_work_item_id
                           ),
                           1
                       ) AS next_attempt_number,
                       operation.correlation_id
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
                WHERE work.tenant_id = %s
                  AND work.processing_work_item_id = %s
                  AND work.processing_operation_id = %s
                  AND work.state = 'queued'
                  AND work.available_at <= now()
                  AND (work.next_retry_at IS NULL OR work.next_retry_at <= now())
                  AND work.leased_until IS NULL
                  AND work.current_processing_attempt_id IS NULL
                  AND work.dead_lettered_at IS NULL
                  AND operation.state IN ('queued', 'running')
                  AND operation.cancellation_requested_at IS NULL
                  AND version.version_state = 'current'
                  AND document.state = ANY(%s)
                """,
                (
                    tenant_id,
                    processing_work_item_id,
                    processing_operation_id,
                    list(_ELIGIBLE_DOCUMENT_STATES),
                ),
            )
            row = cursor.fetchone()
            if row is None:
                return None

            current_fence = int(row[16])
            next_fence = int(row[17])
            next_attempt_number = int(row[18])
            correlation_id = str(row[19])

            cursor.execute(
                """
                INSERT INTO document_ai_processing_attempts (
                    processing_attempt_id, tenant_id, processing_work_item_id,
                    attempt_number, state, worker_id, fencing_token,
                    lease_expires_at, last_heartbeat_at
                ) VALUES (%s, %s, %s, %s, 'running', %s, %s, now() + %s::interval, now())
                """,
                (
                    attempt_id,
                    tenant_id,
                    processing_work_item_id,
                    next_attempt_number,
                    worker_id,
                    next_fence,
                    interval,
                ),
            )
            cursor.execute(
                """
                UPDATE document_ai_processing_work_items AS work
                SET state = 'leased',
                    fencing_token = %s,
                    current_processing_attempt_id = %s,
                    first_attempted_at = COALESCE(work.first_attempted_at, now()),
                    lease_issued_at = now(),
                    last_heartbeat_at = now(),
                    leased_until = now() + %s::interval
                WHERE work.tenant_id = %s
                  AND work.processing_work_item_id = %s
                  AND work.processing_operation_id = %s
                  AND work.state = 'queued'
                  AND work.available_at <= now()
                  AND (work.next_retry_at IS NULL OR work.next_retry_at <= now())
                  AND work.leased_until IS NULL
                  AND work.current_processing_attempt_id IS NULL
                  AND work.dead_lettered_at IS NULL
                  AND work.fencing_token = %s
                RETURNING work.tenant_id
                """,
                (
                    next_fence,
                    attempt_id,
                    interval,
                    tenant_id,
                    processing_work_item_id,
                    processing_operation_id,
                    current_fence,
                ),
            )
            claimed_row = cursor.fetchone()
            if claimed_row is None:
                cursor.execute(
                    """
                    DELETE FROM document_ai_processing_attempts
                    WHERE tenant_id = %s AND processing_attempt_id = %s
                    """,
                    (tenant_id, attempt_id),
                )
                return None

            cursor.execute(
                """
                UPDATE document_ai_processing_operations AS operation
                SET state = 'running'
                WHERE operation.tenant_id = %s
                  AND operation.processing_operation_id = %s
                  AND operation.state = 'queued'
                """,
                (tenant_id, processing_operation_id),
            )

            return ProcessingWorkClaimRecord(
                tenant_id=str(row[0]),
                processing_operation_id=UUID(str(row[2])),
                processing_work_item_id=UUID(str(row[1])),
                document_id=UUID(str(row[3])),
                document_version_id=UUID(str(row[4])),
                source_artifact_id=UUID(str(row[5])),
                work_kind=str(row[6]),
                operation_kind=str(row[7]),
                work_state=str(row[8]),
                priority=int(row[9]),
                available_at=row[10],
                created_at=row[11],
                retry_count=int(row[12]),
                max_attempts=int(row[13]),
                next_retry_at=row[14],
                failure_category=str(row[15]) if row[15] is not None else None,
                processing_attempt_id=attempt_id,
                worker_id=worker_id,
                fencing_token=next_fence,
                lease_seconds=self._lease_seconds,
                correlation_id=correlation_id,
            )

        def _reconcile(connection) -> ProcessingWorkClaimRecord | None:  # type: ignore[no-untyped-def]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT work.tenant_id, work.processing_work_item_id,
                           work.processing_operation_id, version.document_id,
                           operation.document_version_id, artifact.source_artifact_id,
                           work.work_kind, operation.operation_kind, work.state,
                           work.priority, work.available_at, work.created_at,
                           work.retry_count, work.max_attempts, work.next_retry_at,
                           work.failure_category, work.fencing_token,
                           attempt.processing_attempt_id, attempt.worker_id,
                           operation.correlation_id
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
                    JOIN document_ai_source_artifacts AS artifact
                      ON artifact.tenant_id = version.tenant_id
                     AND artifact.document_version_id = version.document_version_id
                    WHERE work.tenant_id = %s
                      AND work.processing_work_item_id = %s
                      AND work.processing_operation_id = %s
                      AND work.current_processing_attempt_id = %s
                      AND attempt.worker_id = %s
                      AND work.state = 'leased'
                      AND work.leased_until > now()
                      AND attempt.state = 'running'
                      AND operation.cancellation_requested_at IS NULL
                      AND version.version_state = 'current'
                      AND document.state = ANY(%s)
                    """,
                    (
                        tenant_id,
                        processing_work_item_id,
                        processing_operation_id,
                        attempt_id,
                        worker_id,
                        list(_ELIGIBLE_DOCUMENT_STATES),
                    ),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return ProcessingWorkClaimRecord(
                tenant_id=str(row[0]),
                processing_operation_id=UUID(str(row[2])),
                processing_work_item_id=UUID(str(row[1])),
                document_id=UUID(str(row[3])),
                document_version_id=UUID(str(row[4])),
                source_artifact_id=UUID(str(row[5])),
                work_kind=str(row[6]),
                operation_kind=str(row[7]),
                work_state=str(row[8]),
                priority=int(row[9]),
                available_at=row[10],
                created_at=row[11],
                retry_count=int(row[12]),
                max_attempts=int(row[13]),
                next_retry_at=row[14],
                failure_category=str(row[15]) if row[15] is not None else None,
                processing_attempt_id=UUID(str(row[17])),
                worker_id=str(row[18]),
                fencing_token=int(row[16]),
                lease_seconds=self._lease_seconds,
                correlation_id=str(row[19]),
            )

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.processing_work_claim",
            transaction_callback=_claim,
            reconcile_ambiguous_result=_reconcile,
        )

    def heartbeat(self, *, lease: ProcessingAttemptLease) -> bool:
        """Renew only the exact current, unexpired attempt for one bounded lease interval."""

        interval = f"{lease.lease_seconds} seconds"

        def _heartbeat(cursor) -> bool:  # type: ignore[no-untyped-def]
            cursor.execute(
                """
                WITH valid AS (
                    SELECT work.tenant_id
                    FROM document_ai_processing_work_items AS work
                    JOIN document_ai_processing_attempts AS attempt
                      ON attempt.tenant_id = work.tenant_id
                     AND attempt.processing_attempt_id = work.current_processing_attempt_id
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                    WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                      AND work.current_processing_attempt_id = %s AND work.fencing_token = %s
                      AND work.state = 'leased' AND work.leased_until > now()
                      AND attempt.state = 'running' AND attempt.fencing_token = %s
                      AND operation.cancellation_requested_at IS NULL
                ), updated_work AS (
                    UPDATE document_ai_processing_work_items AS work
                    SET leased_until = now() + %s::interval, last_heartbeat_at = now()
                    FROM valid
                    WHERE work.tenant_id = valid.tenant_id
                      AND work.processing_work_item_id = %s
                    RETURNING work.tenant_id
                ) UPDATE document_ai_processing_attempts AS attempt
                SET lease_expires_at = now() + %s::interval, last_heartbeat_at = now()
                FROM updated_work
                WHERE attempt.tenant_id = updated_work.tenant_id
                  AND attempt.processing_attempt_id = %s
                RETURNING attempt.processing_attempt_id
                """,
                (
                    lease.tenant_id,
                    lease.processing_work_item_id,
                    lease.processing_attempt_id,
                    lease.fencing_token,
                    lease.fencing_token,
                    interval,
                    lease.processing_work_item_id,
                    interval,
                    lease.processing_attempt_id,
                ),
            )
            return cursor.fetchone() is not None

        def _reconcile(connection) -> bool | None:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT attempt.processing_attempt_id
                    FROM document_ai_processing_work_items AS work
                    JOIN document_ai_processing_attempts AS attempt
                      ON attempt.tenant_id = work.tenant_id
                     AND attempt.processing_attempt_id = work.current_processing_attempt_id
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                    WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                      AND work.current_processing_attempt_id = %s AND work.fencing_token = %s
                      AND work.state = 'leased' AND work.leased_until > now()
                      AND attempt.state = 'running' AND attempt.fencing_token = %s
                      AND operation.cancellation_requested_at IS NULL
                    """,
                    (
                        lease.tenant_id,
                        lease.processing_work_item_id,
                        lease.processing_attempt_id,
                        lease.fencing_token,
                        lease.fencing_token,
                    ),
                )
                return cursor.fetchone() is not None

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.processing_worker.heartbeat",
            transaction_callback=_heartbeat,
            reconcile_ambiguous_result=_reconcile,
        )

    def checkpoint(
        self,
        *,
        lease: ProcessingAttemptLease,
        checkpoint_key: str,
        sequence: int,
        payload: dict[str, object],
    ) -> bool:
        """Persist only a newer checkpoint under the current valid fence."""

        if not checkpoint_key.strip() or sequence < 1:
            raise ValueError("processing_checkpoint_invalid")
        payload_json = json.dumps(payload, sort_keys=True)

        def _checkpoint(cursor) -> bool:  # type: ignore[no-untyped-def]
            cursor.execute(
                """
                WITH valid AS (
                    SELECT work.tenant_id
                    FROM document_ai_processing_work_items AS work
                    JOIN document_ai_processing_attempts AS attempt
                      ON attempt.tenant_id = work.tenant_id
                     AND attempt.processing_attempt_id = work.current_processing_attempt_id
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                    WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                      AND work.current_processing_attempt_id = %s
                      AND work.fencing_token = %s AND work.state = 'leased'
                      AND work.leased_until > now() AND attempt.state = 'running'
                      AND attempt.fencing_token = %s
                      AND operation.cancellation_requested_at IS NULL
                ), written AS (
                    INSERT INTO document_ai_processing_checkpoints (
                        tenant_id, processing_attempt_id, checkpoint_key, checkpoint_payload,
                        sequence, updated_at
                    )
                    SELECT tenant_id, %s, %s, %s::jsonb, %s, now() FROM valid
                    ON CONFLICT (tenant_id, processing_attempt_id, checkpoint_key) DO UPDATE
                    SET checkpoint_payload = EXCLUDED.checkpoint_payload,
                        sequence = EXCLUDED.sequence, updated_at = now()
                    WHERE document_ai_processing_checkpoints.sequence < EXCLUDED.sequence
                    RETURNING processing_checkpoint_id
                )
                UPDATE document_ai_processing_attempts AS attempt
                SET checkpoint_sequence = GREATEST(attempt.checkpoint_sequence, %s)
                FROM valid
                WHERE attempt.tenant_id = valid.tenant_id
                  AND attempt.processing_attempt_id = %s
                  AND EXISTS (SELECT 1 FROM written)
                RETURNING attempt.processing_attempt_id
                """,
                (
                    lease.tenant_id,
                    lease.processing_work_item_id,
                    lease.processing_attempt_id,
                    lease.fencing_token,
                    lease.fencing_token,
                    lease.processing_attempt_id,
                    checkpoint_key,
                    payload_json,
                    sequence,
                    sequence,
                    lease.processing_attempt_id,
                ),
            )
            if cursor.fetchone() is not None:
                return True
            cursor.execute(
                """
                SELECT checkpoint.sequence, checkpoint.checkpoint_payload
                FROM document_ai_processing_checkpoints AS checkpoint
                WHERE checkpoint.tenant_id = %s
                  AND checkpoint.processing_attempt_id = %s
                  AND checkpoint.checkpoint_key = %s
                """,
                (lease.tenant_id, lease.processing_attempt_id, checkpoint_key),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            return int(row[0]) == sequence and dict(row[1]) == payload

        def _reconcile(connection) -> bool | None:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT checkpoint.sequence, checkpoint.checkpoint_payload
                    FROM document_ai_processing_checkpoints AS checkpoint
                    WHERE checkpoint.tenant_id = %s
                      AND checkpoint.processing_attempt_id = %s
                      AND checkpoint.checkpoint_key = %s
                    """,
                    (lease.tenant_id, lease.processing_attempt_id, checkpoint_key),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return int(row[0]) == sequence and dict(row[1]) == payload

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.processing_worker.checkpoint",
            transaction_callback=_checkpoint,
            reconcile_ambiguous_result=_reconcile,
        )

    def latest_checkpoint(
        self, *, tenant_id: str, processing_work_item_id: UUID
    ) -> DurableCheckpoint | None:
        """Return the newest durable checkpoint across historical attempts for recovery."""

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT checkpoint.checkpoint_key, checkpoint.sequence,
                           checkpoint.checkpoint_payload
                    FROM document_ai_processing_checkpoints AS checkpoint
                    JOIN document_ai_processing_attempts AS attempt
                      ON attempt.tenant_id = checkpoint.tenant_id
                     AND attempt.processing_attempt_id = checkpoint.processing_attempt_id
                    WHERE attempt.tenant_id = %s AND attempt.processing_work_item_id = %s
                    ORDER BY checkpoint.sequence DESC, checkpoint.updated_at DESC LIMIT 1""",
                    (tenant_id, processing_work_item_id),
                )
                row = cursor.fetchone()
        return None if row is None else DurableCheckpoint(str(row[0]), int(row[1]), dict(row[2]))

    def commit_success(self, *, lease: ProcessingAttemptLease, result_reference: str) -> bool:
        """Fence a success commit; cancellation, expiry, and stale attempts are rejected."""

        if not result_reference.strip():
            raise ValueError("processing_result_reference_required")
        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT attempt.state, attempt.fencing_token, work.state,
                           operation.state, operation.result_reference
                    FROM document_ai_processing_attempts AS attempt
                    JOIN document_ai_processing_work_items AS work
                      ON work.tenant_id = attempt.tenant_id
                     AND work.processing_work_item_id = attempt.processing_work_item_id
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                    WHERE attempt.tenant_id = %s
                      AND attempt.processing_attempt_id = %s
                    """,
                    (lease.tenant_id, lease.processing_attempt_id),
                )
                replay_row = cursor.fetchone()
        if replay_row is not None:
            if (
                str(replay_row[0]) == "succeeded"
                and int(replay_row[1]) == lease.fencing_token
                and str(replay_row[2]) == "succeeded"
                and str(replay_row[3]) == "succeeded"
            ):
                return str(replay_row[4]) == result_reference
            if str(replay_row[0]) in {"failed", "cancelled"} or str(replay_row[2]) in {
                "failed",
                "cancelled",
            }:
                return False
        return self._commit_terminal(
            lease=lease, terminal_state="succeeded", detail=result_reference
        )

    def commit_failure(
        self,
        *,
        lease: ProcessingAttemptLease,
        failure: RetryClassifiedFailure,
        retry_policy: RetryPolicyConfig = DEFAULT_DOCUMENT_AI_RETRY_POLICY,
        jitter: float = 0.0,
    ) -> ProcessingFailureDisposition | None:
        """Persist retry scheduling or dead-letter disposition under the active fence.

        Queue publication is intentionally separate: a retry creates a new outbox
        intent atomically, so a broker outage cannot consume processing budget.
        """
        failure_details_json = json.dumps(failure.details, sort_keys=True)
        replay_row = self._load_failure_replay_row(
            tenant_id=lease.tenant_id,
            processing_work_item_id=lease.processing_work_item_id,
            processing_attempt_id=lease.processing_attempt_id,
        )
        if replay_row is not None:
            replayed = self._reconcile_failure_commit(
                tenant_id=lease.tenant_id,
                processing_attempt_id=lease.processing_attempt_id,
                replay_row=replay_row,
                failure=failure,
            )
            if replayed is not None:
                return replayed

        def _commit(cursor) -> ProcessingFailureDisposition | None:  # type: ignore[no-untyped-def]
            cursor.execute(
                """
                SELECT work.retry_count, work.max_attempts, work.first_attempted_at,
                       work.max_retry_elapsed_seconds, attempt.attempt_number
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
                WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                  AND work.current_processing_attempt_id = %s AND work.fencing_token = %s
                  AND work.state = 'leased' AND work.leased_until > now()
                  AND attempt.state = 'running' AND operation.cancellation_requested_at IS NULL
                  AND document.state = ANY(%s) AND version.version_state = 'current'
                FOR UPDATE OF work, attempt
                """,
                (
                    lease.tenant_id,
                    lease.processing_work_item_id,
                    lease.processing_attempt_id,
                    lease.fencing_token,
                    list(_ELIGIBLE_DOCUMENT_STATES),
                ),
            )
            row = cursor.fetchone()
            if row is None:
                replay_row = self._load_failure_replay_row(
                    tenant_id=lease.tenant_id,
                    processing_work_item_id=lease.processing_work_item_id,
                    processing_attempt_id=lease.processing_attempt_id,
                )
                if replay_row is None:
                    return None
                return self._reconcile_failure_commit(
                    tenant_id=lease.tenant_id,
                    processing_attempt_id=lease.processing_attempt_id,
                    replay_row=replay_row,
                    failure=failure,
                )
            (
                retry_count,
                max_attempts,
                first_attempted_at,
                max_elapsed,
                attempt_number,
                processing_operation_id,
                processing_work_item_id,
                work_kind,
                operation_kind,
                document_id,
                document_version_id,
                source_artifact_id,
                worker_id,
                fencing_token,
                correlation_id,
                attempt_error_code,
                attempt_error_detail,
            ) = row
            delay_ms = retry_policy.scheduled_delay_ms(
                attempt_count=int(attempt_number),
                retry_after_ms=failure.retry_after_ms,
                jitter=jitter,
            )
            cursor.execute(
                "UPDATE document_ai_processing_attempts SET state = 'failed', "
                "finished_at = now(), error_code = %s, error_detail = %s "
                "WHERE tenant_id = %s AND processing_attempt_id = %s",
                (
                    failure.error_code,
                    failure_details_json,
                    lease.tenant_id,
                    lease.processing_attempt_id,
                ),
            )
            elapsed_ok = first_attempted_at is None
            if first_attempted_at is not None:
                cursor.execute(
                    "SELECT now() - %s <= %s::interval",
                    (first_attempted_at, f"{int(max_elapsed)} seconds"),
                )
                elapsed_row = cursor.fetchone()
                elapsed_ok = elapsed_row is not None and bool(elapsed_row[0])
            can_retry = failure.retryable and int(attempt_number) < int(max_attempts) and elapsed_ok
            if can_retry:
                cursor.execute(
                    """UPDATE document_ai_processing_work_items
                       SET state = 'queued', retry_count = retry_count + 1,
                           first_attempted_at = COALESCE(first_attempted_at, now()),
                           available_at = now() + %s::interval,
                           next_retry_at = now() + %s::interval,
                           failure_category = %s, leased_until = NULL,
                           lease_issued_at = NULL, last_heartbeat_at = NULL,
                           current_processing_attempt_id = NULL
                     WHERE tenant_id = %s AND processing_work_item_id = %s""",
                    (
                        f"{delay_ms / 1000} seconds",
                        f"{delay_ms / 1000} seconds",
                        failure.reason,
                        lease.tenant_id,
                        lease.processing_work_item_id,
                    ),
                )
                cursor.execute(
                    """INSERT INTO document_ai_processing_outbox (
                          tenant_id, processing_operation_id, processing_work_item_id,
                          event_type,
                          payload, routing_key, correlation_id
                       ) VALUES (%s, %s, %s, %s, '{}'::jsonb, 'document_ai.processing', %s)
                       ON CONFLICT (tenant_id, processing_operation_id, event_type)
                       DO NOTHING""",
                    (
                        lease.tenant_id,
                        lease.processing_operation_id,
                        lease.processing_work_item_id,
                        f"processing.retry.{lease.processing_attempt_id}",
                        lease.correlation_id,
                    ),
                )
                cursor.execute(
                    "UPDATE document_ai_processing_operations SET state = 'queued' "
                    "WHERE tenant_id = %s AND processing_operation_id = %s",
                    (lease.tenant_id, lease.processing_operation_id),
                )
                return ProcessingFailureDisposition("queued", True)
            reason = failure.reason if not failure.retryable else "retry_budget_exhausted"
            failure_class = "retry_exhausted" if failure.retryable else "non_retryable_failure"
            diagnostic_payload = {
                "attempt_number": int(attempt_number),
                "correlation_id": lease.correlation_id,
                "error_code": failure.error_code,
                "error_detail": failure.details,
                "failure_class": failure_class,
                "failure_category": failure.reason,
                "message": failure.message,
                "reason_code": reason,
                "retry_count": int(retry_count),
                "retryable": failure.retryable,
            }
            cursor.execute(
                """UPDATE document_ai_processing_work_items
                   SET state = 'dead_letter', failure_category = %s,
                       dead_letter_reason = %s, dead_lettered_at = now(),
                       leased_until = NULL, lease_issued_at = NULL,
                       last_heartbeat_at = NULL, current_processing_attempt_id = NULL
                 WHERE tenant_id = %s AND processing_work_item_id = %s
                   AND state = 'leased' AND current_processing_attempt_id = %s""",
                (
                    failure.reason,
                    reason,
                    lease.tenant_id,
                    lease.processing_work_item_id,
                    lease.processing_attempt_id,
                ),
            )
            cursor.execute(
                """UPDATE document_ai_processing_operations
                   SET state = 'failed', completed_at = now(), failure_category = %s
                 WHERE tenant_id = %s AND processing_operation_id = %s
                   AND state IN ('queued', 'running')
                   AND cancellation_requested_at IS NULL""",
                (reason, lease.tenant_id, lease.processing_operation_id),
            )
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
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s::jsonb, now(), now(), %s::jsonb
                )
                ON CONFLICT (tenant_id, processing_attempt_id) DO NOTHING
                """,
                (
                    lease.tenant_id,
                    processing_operation_id,
                    processing_work_item_id,
                    lease.processing_attempt_id,
                    attempt_number,
                    document_id,
                    document_version_id,
                    source_artifact_id,
                    work_kind,
                    operation_kind,
                    worker_id,
                    fencing_token,
                    failure_class,
                    failure.reason,
                    reason,
                    retry_count,
                    max_attempts,
                    int(max_elapsed),
                    correlation_id,
                    attempt_error_code,
                    json.dumps(attempt_error_detail if attempt_error_detail is not None else {}),
                    json.dumps(diagnostic_payload, sort_keys=True),
                ),
            )
            return ProcessingFailureDisposition("dead_letter", False)

        def _reconcile(connection) -> ProcessingFailureDisposition | None:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT work.state, work.failure_category, work.dead_letter_reason,
                           work.dead_lettered_at, work.retry_count,
                           attempt.state, attempt.error_code, attempt.error_detail,
                           operation.state, operation.completed_at, operation.failure_category
                    FROM document_ai_processing_work_items AS work
                    JOIN document_ai_processing_attempts AS attempt
                      ON attempt.tenant_id = work.tenant_id
                     AND attempt.processing_attempt_id = %s
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                    WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                    """,
                    (lease.processing_attempt_id, lease.tenant_id, lease.processing_work_item_id),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return self._reconcile_failure_commit(replay_row=row, failure=failure)

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.processing_worker.failure_commit",
            transaction_callback=_commit,
            reconcile_ambiguous_result=_reconcile,
        )

    def recover_dead_letter(
        self, *, tenant_id: str, processing_work_item_id: UUID, correlation_id: str
    ) -> bool:
        """Perform the narrowly governed, tenant-scoped terminal-work recovery."""

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE document_ai_processing_work_items AS work SET state = 'queued',
                           retry_count = 0, first_attempted_at = NULL, next_retry_at = NULL,
                           available_at = now(), dead_lettered_at = NULL, dead_letter_reason = NULL,
                           manual_recovery_count = manual_recovery_count + 1
                         FROM document_ai_processing_operations AS operation
                         JOIN document_ai_document_versions AS version
                           ON version.tenant_id = operation.tenant_id
                          AND version.document_version_id = operation.document_version_id
                         JOIN document_ai_documents AS document
                           ON document.tenant_id = version.tenant_id
                          AND document.document_id = version.document_id
                        WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                          AND work.state = 'dead_letter' AND operation.tenant_id = work.tenant_id
                          AND operation.processing_operation_id = work.processing_operation_id
                          AND operation.cancellation_requested_at IS NULL
                          AND document.state = ANY(%s) AND version.version_state = 'current'
                     RETURNING work.processing_operation_id""",
                    (tenant_id, processing_work_item_id, list(_ELIGIBLE_DOCUMENT_STATES)),
                )
                row = cursor.fetchone()
                if row is None:
                    connection.rollback()
                    return False
                operation_id = row[0]
                cursor.execute(
                    """UPDATE document_ai_processing_operations
                       SET state = 'queued', completed_at = NULL, failure_category = NULL
                     WHERE tenant_id = %s AND processing_operation_id = %s""",
                    (tenant_id, operation_id),
                )
                cursor.execute(
                    """INSERT INTO document_ai_processing_outbox (
                                  tenant_id, processing_operation_id, processing_work_item_id,
                                  event_type,
                                  payload, routing_key, correlation_id
                               ) VALUES (
                                   %s, %s, %s, %s, '{}'::jsonb, 'document_ai.processing', %s
                               )""",
                    (
                        tenant_id,
                        operation_id,
                        processing_work_item_id,
                        f"processing.manual_recovery.{uuid4()}",
                        correlation_id,
                    ),
                )
            connection.commit()
        return True

    def _guarded_update(self, *, lease: ProcessingAttemptLease) -> bool:
        return self.heartbeat(lease=lease)

    def _commit_terminal(
        self, *, lease: ProcessingAttemptLease, terminal_state: str, detail: str
    ) -> bool:
        field = "result_reference" if terminal_state == "succeeded" else "failure_category"

        def _commit(cursor) -> bool:  # type: ignore[no-untyped-def]
            cursor.execute(
                """
                SELECT work.tenant_id
                FROM document_ai_processing_work_items AS work
                JOIN document_ai_processing_attempts AS attempt
                  ON attempt.tenant_id = work.tenant_id
                 AND attempt.processing_attempt_id = work.current_processing_attempt_id
                JOIN document_ai_processing_operations AS operation
                  ON operation.tenant_id = work.tenant_id
                 AND operation.processing_operation_id = work.processing_operation_id
                WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                  AND work.current_processing_attempt_id = %s AND work.fencing_token = %s
                  AND work.state = 'leased' AND work.leased_until > now()
                  AND attempt.state = 'running' AND attempt.fencing_token = %s
                  AND operation.state IN ('queued', 'running')
                  AND operation.cancellation_requested_at IS NULL
                FOR UPDATE OF work, attempt
                """,
                (
                    lease.tenant_id,
                    lease.processing_work_item_id,
                    lease.processing_attempt_id,
                    lease.fencing_token,
                    lease.fencing_token,
                ),
            )
            valid_row = cursor.fetchone()
            if valid_row is None:
                return False

            cursor.execute(
                """
                UPDATE document_ai_processing_attempts AS attempt
                SET state = %s, finished_at = now(),
                    error_code = CASE WHEN %s = 'failed' THEN %s ELSE NULL END
                WHERE attempt.tenant_id = %s
                  AND attempt.processing_attempt_id = %s
                  AND attempt.state = 'running'
                RETURNING attempt.tenant_id
                """,
                (
                    terminal_state,
                    terminal_state,
                    detail,
                    lease.tenant_id,
                    lease.processing_attempt_id,
                ),
            )
            if cursor.fetchone() is None:
                return False

            cursor.execute(
                """
                UPDATE document_ai_processing_work_items AS work
                SET state = %s,
                    leased_until = NULL, lease_issued_at = NULL, last_heartbeat_at = NULL,
                    current_processing_attempt_id = NULL
                WHERE work.tenant_id = %s
                  AND work.processing_work_item_id = %s
                RETURNING work.tenant_id
                """,
                (terminal_state, lease.tenant_id, lease.processing_work_item_id),
            )
            if cursor.fetchone() is None:
                return False

            cursor.execute(
                sql.SQL(
                    """
                    UPDATE document_ai_processing_operations AS operation
                    SET state = %s, completed_at = now(), {field} = %s
                    WHERE operation.tenant_id = %s
                      AND operation.processing_operation_id = %s
                      AND operation.state IN ('queued', 'running')
                      AND operation.cancellation_requested_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM document_ai_processing_work_items AS sibling
                          WHERE sibling.tenant_id = operation.tenant_id
                            AND sibling.processing_operation_id =
                                operation.processing_operation_id
                            AND sibling.state <> %s
                      )
                    RETURNING operation.tenant_id
                    """
                ).format(field=sql.Identifier(field)),
                (
                    terminal_state,
                    detail,
                    lease.tenant_id,
                    lease.processing_operation_id,
                    terminal_state,
                ),
            )
            return cursor.fetchone() is not None

        def _reconcile(connection) -> bool | None:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT attempt.state, attempt.fencing_token, attempt.error_code,
                           attempt.error_detail, operation.state, operation.result_reference,
                           operation.failure_category, work.state, work.failure_category,
                           work.dead_letter_reason
                    FROM document_ai_processing_attempts AS attempt
                    JOIN document_ai_processing_work_items AS work
                      ON work.tenant_id = attempt.tenant_id
                     AND work.processing_work_item_id = attempt.processing_work_item_id
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = work.tenant_id
                     AND operation.processing_operation_id = work.processing_operation_id
                    WHERE attempt.tenant_id = %s
                      AND attempt.processing_attempt_id = %s
                    """,
                    (lease.tenant_id, lease.processing_attempt_id),
                )
                replay_row = cursor.fetchone()
            if replay_row is None:
                return None
            if terminal_state == "succeeded":
                return (
                    str(replay_row[0]) == "succeeded"
                    and int(replay_row[1]) == lease.fencing_token
                    and str(replay_row[4]) == "succeeded"
                    and str(replay_row[5]) == detail
                )
            return self._reconcile_failure_commit(
                replay_row=replay_row,
                failure=RetryClassifiedFailure(
                    classification="transient",
                    error_code=str(replay_row[2]) if replay_row[2] is not None else "",
                    message="",
                    reason=str(replay_row[8]) if replay_row[8] is not None else str(detail),
                    retryable=str(replay_row[7]) == "queued",
                    details=dict(replay_row[3]) if replay_row[3] is not None else {},
                ),
            )

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name=f"document_ai.processing_worker.{terminal_state}",
            transaction_callback=_commit,
            reconcile_ambiguous_result=_reconcile,
        )

    def _reconcile_failure_commit(
        self,
        *,
        tenant_id: str,
        processing_attempt_id: UUID,
        replay_row: tuple[object, ...],
        failure: RetryClassifiedFailure,
    ) -> ProcessingFailureDisposition | None:
        """Validate a replayed failure commit against the durable attempt state."""

        work_state = str(replay_row[0])
        work_failure_category = (
            str(replay_row[1]) if replay_row[1] is not None else None
        )
        dead_letter_reason = str(replay_row[2]) if replay_row[2] is not None else None
        attempt_state = str(replay_row[5])
        attempt_error_code = str(replay_row[6]) if replay_row[6] is not None else None
        attempt_error_detail = replay_row[7]
        operation_state = str(replay_row[8])
        operation_failure_category = (
            str(replay_row[10]) if replay_row[10] is not None else None
        )
        expected_detail = _normalize_json_mapping(failure.details)
        replay_detail = _normalize_json_mapping(attempt_error_detail)
        if failure.retryable:
            if (
                work_state == "queued"
                and work_failure_category == failure.reason
                and attempt_state == "failed"
                and attempt_error_code == failure.error_code
                and replay_detail == expected_detail
                and operation_state == "queued"
            ):
                return ProcessingFailureDisposition("queued", True)
            expected_reason = "retry_budget_exhausted"
            dead_letter_record = self._load_dead_letter_record(
                tenant_id=tenant_id,
                processing_attempt_id=processing_attempt_id,
            )
            if dead_letter_record is None:
                return None
            if (
                work_state == "dead_letter"
                and work_failure_category == failure.reason
                and dead_letter_reason == expected_reason
                and attempt_state == "failed"
                and attempt_error_code == failure.error_code
                and replay_detail == expected_detail
                and operation_state == "failed"
                and operation_failure_category == expected_reason
                and dead_letter_record.failure_class == "retry_exhausted"
                and dead_letter_record.reason_code == expected_reason
                and dead_letter_record.failure_category == failure.reason
                and dead_letter_record.error_code == failure.error_code
                and dead_letter_record.error_detail == expected_detail
            ):
                return ProcessingFailureDisposition("dead_letter", False)
            return None
        expected_reason = failure.reason
        dead_letter_record = self._load_dead_letter_record(
            tenant_id=tenant_id,
            processing_attempt_id=processing_attempt_id,
        )
        if dead_letter_record is None:
            return None
        if (
            work_state == "dead_letter"
            and work_failure_category == failure.reason
            and dead_letter_reason == expected_reason
            and attempt_state == "failed"
            and attempt_error_code == failure.error_code
            and replay_detail == expected_detail
            and operation_state == "failed"
            and operation_failure_category == expected_reason
            and dead_letter_record.failure_class == "non_retryable_failure"
            and dead_letter_record.reason_code == expected_reason
            and dead_letter_record.failure_category == failure.reason
            and dead_letter_record.error_code == failure.error_code
            and dead_letter_record.error_detail == expected_detail
        ):
            return ProcessingFailureDisposition("dead_letter", False)
        return None


def _normalize_json_mapping(value: object) -> dict[str, object] | None:
    """Return a deterministic mapping for JSON-compatible database payloads."""

    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else None
    try:
        return dict(value)  # type: ignore[arg-type]
    except Exception:
        return None


class ProcessingQueueConsumer:
    """Consumes redeliverable messages without treating delivery as ownership."""

    def __init__(
        self,
        *,
        repository: ProcessingWorkerRepository,
        worker_id: str,
        executor: ProcessingWorkExecutor,
    ) -> None:
        self._repository = repository
        self._worker_id = worker_id
        self._executor = executor

    def handle(self, delivery: QueueDeliveryProtocol) -> bool:
        self._repository.recover_expired_leases()
        lease = self._repository.claim(message=delivery.message, worker_id=self._worker_id)
        if lease is None:
            delivery.acknowledge()  # Durable state says this duplicate needs no execution.
            return True
        checkpoint = self._repository.latest_checkpoint(
            tenant_id=lease.tenant_id, processing_work_item_id=lease.processing_work_item_id
        )
        try:
            result_reference = self._executor.execute(lease=lease, checkpoint=checkpoint)
        except Exception as error:  # Failure is classified before durable disposition.
            failure = classify_document_ai_failure(error=error)
            disposition = self._repository.commit_failure(lease=lease, failure=failure)
            if disposition is None:
                return False
            metric_id = (
                DOCUMENT_PROCESSING_RETRIES_TOTAL
                if disposition.retry_scheduled
                else DOCUMENT_PROCESSING_DEAD_LETTERS_TOTAL
            )
            get_default_metrics_emitter().increment_counter_non_blocking(
                metric_id,
                dimensions={"status": disposition.state, "reason_code": failure.reason},
            )
            emit_document_structured_log(
                event_name="document_processing_failure_disposition",
                action="schedule_retry" if disposition.retry_scheduled else "dead_letter",
                status=disposition.state,
                trace_id=lease.correlation_id,
                correlation_id=lease.correlation_id,
                reason_code=failure.reason,
                payload={"processing_operation_id": str(lease.processing_operation_id)},
            )
            delivery.acknowledge()
            return True
        if not self._repository.commit_success(lease=lease, result_reference=result_reference):
            return False
        delivery.acknowledge()
        return True
