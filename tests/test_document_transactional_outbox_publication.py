"""Milestone 10 durable publication controls."""

from __future__ import annotations

from uuid import uuid4
from typing import cast
from pathlib import Path

import pytest

from services.document_ai.app.outbox import ClaimedOutboxRecord
from services.document_ai.app.outbox import ProcessingOutboxRelay
from services.document_ai.app.outbox import ProcessingWorkMessage
from services.document_ai.app.outbox import QueuePublicationError
from services.document_ai.app.outbox import QueuePublisherProtocol
from services.document_ai.app.outbox import PublicationFailureClass
from services.document_ai.app.outbox import safe_outbox_payload_json
from services.document_ai.app.outbox import ProcessingOutboxRepository


def _record() -> ClaimedOutboxRecord:
    return ClaimedOutboxRecord(
        processing_outbox_id=uuid4(),
        processing_operation_id=uuid4(),
        processing_work_item_id=uuid4(),
        tenant_id="tenant-a",
        correlation_id="correlation-a",
        claim_token=uuid4(),
        attempt_number=1,
    )


def test_milestone_10_schema_keeps_one_outbox_and_persists_attempt_lineage() -> None:
    sql = (
        Path("database/migrations/0036_document_ai_transactional_outbox_publication.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    for marker in (
        "alter table document_ai_processing_outbox",
        "processing_work_item_id",
        "alter column processing_work_item_id set not null",
        "processing_outbox_attempt_id",
        "foreign key (tenant_id, processing_work_item_id)",
        "foreign key (tenant_id, processing_outbox_id)",
        "idx_document_ai_processing_outbox_reconciliation",
        "idx_document_ai_processing_outbox_stale_claim",
    ):
        assert marker in sql


def test_upload_confirmation_links_one_existing_work_item_to_its_outbox_before_commit() -> None:
    source = Path("services/document_ai/app/document_registry.py").read_text(encoding="utf-8")
    transaction = source[source.index("def _register_persistent_confirmation_transaction") :]
    assert "RETURNING processing_work_item_id" in transaction
    assert "processing_work_item_id, event_type" in transaction
    assert transaction.index("INSERT INTO document_ai_processing_outbox") < transaction.index(
        "INSERT INTO document_ai_completion_idempotency"
    )


def test_safe_message_uses_stable_references_and_excludes_document_content_and_storage_keys() -> (
    None
):
    record = _record()
    message = record.message()
    assert isinstance(message, ProcessingWorkMessage)
    payload = safe_outbox_payload_json(message)
    assert str(record.processing_outbox_id) in payload
    assert str(record.processing_operation_id) in payload
    assert str(record.processing_work_item_id) in payload
    assert "object_key" not in payload
    assert "checksum" not in payload
    assert "document_content" not in payload


class _Repository:
    def __init__(self, record: ClaimedOutboxRecord) -> None:
        self.record = record
        self.acknowledged: list[str] = []
        self.failures: list[QueuePublicationError] = []

    def claim_due(self, *, limit: int) -> tuple[ClaimedOutboxRecord, ...]:
        assert limit == 100
        return (self.record,)

    def acknowledge(self, *, record: ClaimedOutboxRecord, broker_message_id: str) -> bool:
        assert record == self.record
        self.acknowledged.append(broker_message_id)
        return True

    def record_failure(self, *, record: ClaimedOutboxRecord, error: QueuePublicationError) -> bool:
        assert record == self.record
        self.failures.append(error)
        return True


class _Publisher:
    def __init__(self, outcome: str | Exception) -> None:
        self.outcome = outcome

    def publish(self, message: ProcessingWorkMessage) -> str:
        assert message.processing_work_item_id
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_broker_acknowledgement_marks_only_the_claimed_durable_record_complete() -> None:
    repository = _Repository(_record())
    relay = ProcessingOutboxRelay(
        repository=cast(ProcessingOutboxRepository, repository),
        publisher=cast(QueuePublisherProtocol, _Publisher("broker-message-1")),
    )
    assert relay.reconcile_once() == 1
    assert repository.acknowledged == ["broker-message-1"]
    assert repository.failures == []


@pytest.mark.parametrize(
    ("error_code", "failure_class"),
    (("queue_timeout", "transient"), ("invalid_routing", "permanent")),
)
def test_publication_failures_remain_explicit_for_durable_reconciliation(
    error_code: str, failure_class: str
) -> None:
    repository = _Repository(_record())
    error = QueuePublicationError(
        error_code=error_code,
        failure_class=cast_failure_class(failure_class),
    )
    relay = ProcessingOutboxRelay(
        repository=cast(ProcessingOutboxRepository, repository),
        publisher=cast(QueuePublisherProtocol, _Publisher(error)),
    )
    assert relay.reconcile_once() == 0
    assert repository.failures == [error]


def cast_failure_class(value: str) -> PublicationFailureClass:
    if value not in {"transient", "permanent"}:
        raise AssertionError(value)
    return cast(PublicationFailureClass, value)
