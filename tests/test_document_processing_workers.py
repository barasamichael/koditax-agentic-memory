"""Milestone 11 durable worker lease, fencing, checkpoint, and recovery controls."""

from __future__ import annotations

from uuid import uuid4
from typing import cast
from pathlib import Path

from services.document_ai.app.outbox import ProcessingWorkMessage
from services.document_ai.app.processing_workers import DurableCheckpoint
from services.document_ai.app.processing_workers import QueueDeliveryProtocol
from services.document_ai.app.processing_workers import ProcessingAttemptLease
from services.document_ai.app.processing_workers import ProcessingWorkExecutor
from services.document_ai.app.processing_workers import ProcessingQueueConsumer
from services.document_ai.app.processing_workers import ProcessingWorkerRepository


def _lease() -> ProcessingAttemptLease:
    return ProcessingAttemptLease(
        tenant_id="tenant-a",
        processing_operation_id=uuid4(),
        processing_work_item_id=uuid4(),
        processing_attempt_id=uuid4(),
        worker_id="worker-a",
        fencing_token=2,
        lease_seconds=60,
        correlation_id="correlation-a",
    )


def _message() -> ProcessingWorkMessage:
    return ProcessingWorkMessage(
        processing_outbox_id=uuid4(),
        processing_operation_id=uuid4(),
        processing_work_item_id=uuid4(),
        tenant_id="tenant-a",
        correlation_id="correlation-a",
    )


def test_worker_migration_adds_durable_lease_fence_and_checkpoint_columns() -> None:
    sql = Path("database/migrations/0037_document_ai_worker_leases.sql").read_text().lower()
    for marker in (
        "current_processing_attempt_id",
        "fencing_token",
        "last_heartbeat_at",
        "lease_expires_at",
        "checkpoint_sequence",
        "processing_checkpoints",
        "idx_document_ai_processing_work_items_lease_recovery",
    ):
        assert marker in sql


def test_worker_repository_uses_fenced_current_attempt_for_every_mutation() -> None:
    source = Path("services/document_ai/app/processing_workers.py").read_text()
    assert "current_processing_attempt_id = %s" in source
    assert "work.fencing_token = %s" in source
    assert "work.leased_until > now()" in source
    assert "operation.cancellation_requested_at IS NULL" in source
    assert "document.state = ANY(%s)" in source
    assert "checkpoint_sequence = GREATEST" in source
    assert "ORDER BY checkpoint.sequence DESC" in source
    assert "sibling.state <> %s" in source
    assert "DELETE FROM document_ai_processing_attempts" in source
    assert "execute_document_ai_database_transaction" in source
    assert "SKIP LOCKED" not in source


class _Repository:
    def __init__(self, lease: ProcessingAttemptLease | None, commit: bool = True) -> None:
        self.lease = lease
        self.commit = commit
        self.recovered = 0

    def recover_expired_leases(self) -> int:
        self.recovered += 1
        return 0

    def claim(
        self, *, message: ProcessingWorkMessage, worker_id: str
    ) -> ProcessingAttemptLease | None:
        assert message.tenant_id == "tenant-a"
        assert worker_id == "worker-a"
        return self.lease

    def latest_checkpoint(
        self, *, tenant_id: str, processing_work_item_id: object
    ) -> DurableCheckpoint:
        assert tenant_id == "tenant-a"
        return DurableCheckpoint("page", 4, {"completed": [1, 2, 3, 4]})

    def commit_success(self, *, lease: ProcessingAttemptLease, result_reference: str) -> bool:
        assert lease == self.lease
        assert result_reference == "result:durable"
        return self.commit


class _Delivery:
    def __init__(self) -> None:
        self.message = _message()
        self.acknowledged = 0

    def acknowledge(self) -> None:
        self.acknowledged += 1


class _Executor:
    def __init__(self) -> None:
        self.checkpoint: DurableCheckpoint | None = None

    def execute(
        self, *, lease: ProcessingAttemptLease, checkpoint: DurableCheckpoint | None
    ) -> str:
        self.checkpoint = checkpoint
        return "result:durable"


def test_duplicate_delivery_is_acknowledged_without_a_second_attempt() -> None:
    delivery = _Delivery()
    repository = _Repository(None)
    consumer = ProcessingQueueConsumer(
        repository=cast(ProcessingWorkerRepository, repository),
        worker_id="worker-a",
        executor=cast(ProcessingWorkExecutor, _Executor()),
    )
    assert consumer.handle(cast(QueueDeliveryProtocol, delivery))
    assert repository.recovered == 1
    assert delivery.acknowledged == 1


def test_recovered_attempt_resumes_checkpoint_and_acknowledges_only_after_commit() -> None:
    lease = _lease()
    delivery = _Delivery()
    repository = _Repository(lease, commit=True)
    executor = _Executor()
    consumer = ProcessingQueueConsumer(
        repository=cast(ProcessingWorkerRepository, repository),
        worker_id="worker-a",
        executor=cast(ProcessingWorkExecutor, executor),
    )
    assert consumer.handle(cast(QueueDeliveryProtocol, delivery))
    assert executor.checkpoint is not None and executor.checkpoint.sequence == 4
    assert delivery.acknowledged == 1


def test_stale_result_rejection_leaves_delivery_unacknowledged_for_recovery() -> None:
    delivery = _Delivery()
    repository = _Repository(_lease(), commit=False)
    consumer = ProcessingQueueConsumer(
        repository=cast(ProcessingWorkerRepository, repository),
        worker_id="worker-a",
        executor=cast(ProcessingWorkExecutor, _Executor()),
    )
    assert not consumer.handle(cast(QueueDeliveryProtocol, delivery))
    assert delivery.acknowledged == 0
