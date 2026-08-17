"""Transactional publication of durable document processing work."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from typing import Literal
from typing import Protocol
from hashlib import sha256
from datetime import timedelta
from dataclasses import dataclass

from services.document_ai.app.metrics import get_default_metrics_emitter
from services.document_ai.app.metrics import DOCUMENT_OUTBOX_PUBLICATIONS_TOTAL
from services.document_ai.app.metrics import DOCUMENT_OUTBOX_PUBLICATION_FAILURES_TOTAL
from services.document_ai.app.logging_context import emit_document_structured_log
from services.document_ai.app.persistence_support import connect_document_ai_database

OUTBOX_ROUTING_KEY = "document_ai.processing"
PublicationFailureClass = Literal["transient", "permanent"]


class QueuePublicationError(RuntimeError):
    """Classify a failed broker publication without exposing broker internals."""

    def __init__(self, *, error_code: str, failure_class: PublicationFailureClass) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.failure_class = failure_class


@dataclass(frozen=True)
class ProcessingWorkMessage:
    """Safe, stable queue payload; consumers must reload durable state before work."""

    processing_outbox_id: UUID
    processing_operation_id: UUID
    processing_work_item_id: UUID
    tenant_id: str
    correlation_id: str
    routing_key: Literal["document_ai.processing"] = OUTBOX_ROUTING_KEY

    def as_payload(self) -> dict[str, str]:
        return {
            "processing_outbox_id": str(self.processing_outbox_id),
            "processing_operation_id": str(self.processing_operation_id),
            "processing_work_item_id": str(self.processing_work_item_id),
            "tenant_id": self.tenant_id,
            "correlation_id": self.correlation_id,
            "routing_key": self.routing_key,
        }


class QueuePublisherProtocol(Protocol):
    """Configured broker boundary; success is its established publication acknowledgement."""

    def publish(self, message: ProcessingWorkMessage) -> str: ...


@dataclass(frozen=True)
class ClaimedOutboxRecord:
    """A single database-claimed publication intent."""

    processing_outbox_id: UUID
    processing_operation_id: UUID
    processing_work_item_id: UUID
    tenant_id: str
    correlation_id: str
    claim_token: UUID
    attempt_number: int

    def message(self) -> ProcessingWorkMessage:
        return ProcessingWorkMessage(
            processing_outbox_id=self.processing_outbox_id,
            processing_operation_id=self.processing_operation_id,
            processing_work_item_id=self.processing_work_item_id,
            tenant_id=self.tenant_id,
            correlation_id=self.correlation_id,
        )


class ProcessingOutboxRepository:
    """PostgreSQL authority for claims, attempts, acknowledgements and recovery."""

    def __init__(self, *, database_url: str, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("outbox_max_attempts_must_be_positive")
        self._database_url = database_url
        self._max_attempts = max_attempts

    def recover_stale_claims(self, *, stale_after: timedelta) -> int:
        """Return crashed publisher claims to retryable state without losing their lineage."""

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_ai_processing_outbox
                    SET state = 'pending', claimed_at = NULL, claim_token = NULL,
                        next_attempt_at = now()
                    WHERE state = 'publishing' AND claimed_at < now() - %s::interval
                    """,
                    (f"{int(stale_after.total_seconds())} seconds",),
                )
                recovered = cursor.rowcount
            connection.commit()
        return recovered

    def claim_due(self, *, limit: int) -> tuple[ClaimedOutboxRecord, ...]:
        """Claim due intents with SKIP LOCKED so concurrent relays cannot race freely."""

        if limit < 1:
            return ()
        claim_token = uuid4()
        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH ranked_due AS (
                        SELECT outbox.processing_outbox_id, work.priority,
                               row_number() OVER (
                                   PARTITION BY outbox.tenant_id
                                   ORDER BY work.priority DESC, outbox.next_attempt_at ASC,
                                            outbox.created_at ASC
                               ) AS tenant_rank
                        FROM document_ai_processing_outbox AS outbox
                        JOIN document_ai_processing_work_items AS work
                          ON work.tenant_id = outbox.tenant_id
                         AND work.processing_work_item_id = outbox.processing_work_item_id
                        WHERE outbox.state IN ('pending', 'failed')
                          AND outbox.next_attempt_at <= now()
                          AND outbox.publish_attempts < %s
                          AND outbox.last_error_class IS DISTINCT FROM 'permanent'
                          AND work.state = 'queued' AND work.available_at <= now()
                    ), selected_due AS (
                        -- Round-robin tenant ranks prevent a noisy tenant from monopolising
                        -- publication; priority still wins within each tenant's turn.
                        SELECT processing_outbox_id FROM ranked_due
                        ORDER BY tenant_rank ASC, priority DESC, processing_outbox_id
                        LIMIT %s
                    ), due AS (
                        SELECT outbox.processing_outbox_id
                        FROM document_ai_processing_outbox AS outbox
                        JOIN selected_due
                          ON selected_due.processing_outbox_id = outbox.processing_outbox_id
                        FOR UPDATE OF outbox SKIP LOCKED
                    )
                    UPDATE document_ai_processing_outbox AS outbox
                    SET state = 'publishing', claimed_at = now(), claim_token = %s,
                        publish_attempts = publish_attempts + 1
                    FROM due, document_ai_processing_operations AS operation
                    WHERE outbox.processing_outbox_id = due.processing_outbox_id
                      AND operation.tenant_id = outbox.tenant_id
                      AND operation.processing_operation_id = outbox.processing_operation_id
                    RETURNING outbox.processing_outbox_id, outbox.processing_operation_id,
                              outbox.processing_work_item_id, outbox.tenant_id,
                              COALESCE(outbox.correlation_id, operation.correlation_id),
                              outbox.claim_token, outbox.publish_attempts
                    """,
                    (self._max_attempts, limit, claim_token),
                )
                rows = cursor.fetchall()
                for row in rows:
                    cursor.execute(
                        """
                        INSERT INTO document_ai_processing_outbox_attempts (
                            tenant_id, processing_outbox_id, attempt_number, claim_token, state
                        ) VALUES (%s, %s, %s, %s, 'attempted')
                        """,
                        (str(row[3]), row[0], int(row[6]), row[5]),
                    )
            connection.commit()
        return tuple(
            ClaimedOutboxRecord(
                processing_outbox_id=UUID(str(row[0])),
                processing_operation_id=UUID(str(row[1])),
                processing_work_item_id=UUID(str(row[2])),
                tenant_id=str(row[3]),
                correlation_id=str(row[4]),
                claim_token=UUID(str(row[5])),
                attempt_number=int(row[6]),
            )
            for row in rows
        )

    def acknowledge(self, *, record: ClaimedOutboxRecord, broker_message_id: str) -> bool:
        """Persist only a broker-confirmed acknowledgement; repeat acknowledgements are safe."""

        if not broker_message_id.strip():
            raise QueuePublicationError(
                error_code="queue_malformed_acknowledgement", failure_class="permanent"
            )
        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_ai_processing_outbox
                    SET state = 'published', published_at = COALESCE(published_at, now()),
                        claimed_at = NULL, claim_token = NULL, last_error_code = NULL,
                        last_error_class = NULL
                    WHERE processing_outbox_id = %s AND tenant_id = %s
                      AND (claim_token = %s OR state = 'published')
                    RETURNING processing_outbox_id
                    """,
                    (record.processing_outbox_id, record.tenant_id, record.claim_token),
                )
                row = cursor.fetchone()
                if row is None:
                    connection.rollback()
                    return False
                cursor.execute(
                    """
                    INSERT INTO document_ai_processing_outbox_attempts (
                        tenant_id, processing_outbox_id, attempt_number, claim_token, state,
                        broker_message_id, acknowledged_at
                    ) VALUES (%s, %s, %s, %s, 'acknowledged', %s, now())
                    ON CONFLICT (tenant_id, processing_outbox_id, attempt_number) DO NOTHING
                    """,
                    (
                        record.tenant_id,
                        record.processing_outbox_id,
                        record.attempt_number,
                        record.claim_token,
                        broker_message_id,
                    ),
                )
            connection.commit()
        return True

    def record_failure(self, *, record: ClaimedOutboxRecord, error: QueuePublicationError) -> bool:
        """Record a bounded retry or explicit permanent failure without discarding intent."""

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_ai_processing_outbox
                    SET state = CASE
                            WHEN %s = 'permanent' OR publish_attempts >= %s THEN 'failed'
                            ELSE 'pending'
                        END,
                        next_attempt_at = CASE
                            WHEN %s = 'permanent' OR publish_attempts >= %s THEN now()
                            ELSE now() + make_interval(
                                secs => 10 * (2 ^ (publish_attempts - 1))::int
                            )
                        END,
                        claimed_at = NULL, claim_token = NULL, last_error_code = %s,
                        last_error_class = %s
                    WHERE processing_outbox_id = %s AND tenant_id = %s AND claim_token = %s
                    RETURNING processing_outbox_id
                    """,
                    (
                        error.failure_class,
                        self._max_attempts,
                        error.failure_class,
                        self._max_attempts,
                        error.error_code,
                        error.failure_class,
                        record.processing_outbox_id,
                        record.tenant_id,
                        record.claim_token,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    connection.rollback()
                    return False
                cursor.execute(
                    """
                    INSERT INTO document_ai_processing_outbox_attempts (
                        tenant_id, processing_outbox_id, attempt_number, claim_token, state,
                        error_code, error_class
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.tenant_id,
                        record.processing_outbox_id,
                        record.attempt_number,
                        record.claim_token,
                        "permanent_failure"
                        if error.failure_class == "permanent"
                        else "transient_failure",
                        error.error_code,
                        error.failure_class,
                    ),
                )
            connection.commit()
        return True


class ProcessingOutboxRelay:
    """Reconcile committed durable work with the configured broker."""

    def __init__(
        self,
        *,
        repository: ProcessingOutboxRepository,
        publisher: QueuePublisherProtocol,
    ) -> None:
        self._repository = repository
        self._publisher = publisher

    def reconcile_once(self, *, limit: int = 100) -> int:
        """Publish due intents. A broker error leaves durable work recoverable."""

        completed = 0
        for record in self._repository.claim_due(limit=limit):
            try:
                acknowledgement = self._publisher.publish(record.message())
                if self._repository.acknowledge(record=record, broker_message_id=acknowledgement):
                    completed += 1
                    get_default_metrics_emitter().increment_counter_non_blocking(
                        DOCUMENT_OUTBOX_PUBLICATIONS_TOTAL,
                        dimensions={"action": "publish", "status": "acknowledged"},
                    )
                    _emit_outbox_log(record=record, status="acknowledged", reason_code=None)
            except QueuePublicationError as error:
                self._repository.record_failure(record=record, error=error)
                _record_publication_failure(record=record, error=error)
            except Exception:
                error = QueuePublicationError(
                    error_code="queue_publication_unavailable", failure_class="transient"
                )
                self._repository.record_failure(record=record, error=error)
                _record_publication_failure(record=record, error=error)
        return completed


def safe_outbox_payload_json(message: ProcessingWorkMessage) -> str:
    """Serialize only the governed reference payload for queue transports that require JSON."""

    return json.dumps(message.as_payload(), sort_keys=True, separators=(",", ":"))


def _record_publication_failure(
    *, record: ClaimedOutboxRecord, error: QueuePublicationError
) -> None:
    get_default_metrics_emitter().increment_counter_non_blocking(
        DOCUMENT_OUTBOX_PUBLICATION_FAILURES_TOTAL,
        dimensions={"action": "publish", "status": error.failure_class},
    )
    _emit_outbox_log(record=record, status=error.failure_class, reason_code=error.error_code)


def _emit_outbox_log(*, record: ClaimedOutboxRecord, status: str, reason_code: str | None) -> None:
    emit_document_structured_log(
        event_name="document_processing_outbox_publication",
        action="publish",
        status=status,
        trace_id=sha256(str(record.processing_outbox_id).encode()).hexdigest(),
        correlation_id=record.correlation_id,
        reason_code=reason_code,
        payload={
            "processing_outbox_id": str(record.processing_outbox_id),
            "processing_operation_id": str(record.processing_operation_id),
            "processing_work_item_id": str(record.processing_work_item_id),
            "attempt_number": record.attempt_number,
        },
    )
