"""CockroachDB-backed discovery of eligible durable processing work."""

from __future__ import annotations

from uuid import UUID
from datetime import datetime
from dataclasses import dataclass

from services.document_ai.app.config import get_document_ai_work_discovery_max_batch_size
from services.document_ai.app.persistence_support import connect_document_ai_database

_ELIGIBLE_DOCUMENT_STATES = ("uploaded", "processing", "validated", "active")
PROCESSING_WORK_DISCOVERY_SQL = """
SELECT work.processing_work_item_id, work.processing_operation_id,
       work.tenant_id, version.document_id,
       operation.document_version_id, artifact.source_artifact_id,
       work.work_kind, operation.operation_kind, work.state,
       work.priority, work.available_at, work.created_at,
       work.retry_count, work.max_attempts, work.next_retry_at,
       work.failure_category
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
WHERE work.state = 'queued'
  AND work.available_at <= now()
  AND (work.next_retry_at IS NULL OR work.next_retry_at <= now())
  AND work.leased_until IS NULL
  AND work.current_processing_attempt_id IS NULL
  AND work.dead_lettered_at IS NULL
  AND operation.state IN ('queued', 'running')
  AND operation.cancellation_requested_at IS NULL
  AND version.version_state = 'current'
  AND document.state = ANY(%s)
ORDER BY work.available_at ASC, work.priority DESC, work.created_at ASC,
         work.processing_work_item_id ASC
LIMIT %s
"""


@dataclass(frozen=True)
class ProcessingWorkCandidate:
    """A minimal durable work candidate returned without any claim mutation."""

    processing_work_item_id: UUID
    processing_operation_id: UUID
    tenant_id: str
    document_id: UUID
    document_version_id: UUID
    source_artifact_id: UUID
    work_kind: str
    operation_kind: str
    state: str
    priority: int
    available_at: datetime
    created_at: datetime
    retry_count: int
    max_attempts: int
    next_retry_at: datetime | None
    failure_category: str | None


class ProcessingWorkDiscoveryRepository:
    """Read-only CockroachDB authority for bounded work discovery."""

    def __init__(self, *, database_url: str, max_batch_size: int | None = None) -> None:
        self._database_url = database_url
        configured_max = (
            get_document_ai_work_discovery_max_batch_size()
            if max_batch_size is None
            else max_batch_size
        )
        if configured_max < 1:
            raise ValueError("document_ai_work_discovery_max_batch_size_must_be_positive")
        self._max_batch_size = configured_max

    @property
    def max_batch_size(self) -> int:
        """Return the configured discovery batch ceiling."""

        return self._max_batch_size

    def discover_work_candidates(self, *, limit: int) -> tuple[ProcessingWorkCandidate, ...]:
        """Return only currently eligible durable work without mutating its state."""

        if limit < 1:
            return ()
        if limit > self._max_batch_size:
            raise ValueError("document_ai_work_discovery_limit_exceeds_maximum")

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    PROCESSING_WORK_DISCOVERY_SQL,
                    (list(_ELIGIBLE_DOCUMENT_STATES), limit),
                )
                rows = cursor.fetchall()
        return tuple(
            ProcessingWorkCandidate(
                processing_work_item_id=UUID(str(row[0])),
                processing_operation_id=UUID(str(row[1])),
                tenant_id=str(row[2]),
                document_id=UUID(str(row[3])),
                document_version_id=UUID(str(row[4])),
                source_artifact_id=UUID(str(row[5])),
                work_kind=str(row[6]),
                operation_kind=str(row[7]),
                state=str(row[8]),
                priority=int(row[9]),
                available_at=row[10],
                created_at=row[11],
                retry_count=int(row[12]),
                max_attempts=int(row[13]),
                next_retry_at=row[14],
                failure_category=str(row[15]) if row[15] is not None else None,
            )
            for row in rows
        )
