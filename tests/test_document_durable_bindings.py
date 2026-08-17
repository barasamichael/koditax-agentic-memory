"""Verify Milestone 7 durable document-binding controls."""

from __future__ import annotations

from uuid import UUID
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.document_ai.app.document_bindings import DocumentBindingRequest
from services.document_ai.app.document_bindings import InMemoryDocumentBindingStore

TENANT_A = "tenant-a"
ACTOR_A = UUID("00000000-0000-0000-0000-000000000001")
ACTOR_B = UUID("00000000-0000-0000-0000-000000000002")
DOCUMENT_A = UUID("00000000-0000-0000-0000-000000000011")
DOCUMENT_B = UUID("00000000-0000-0000-0000-000000000012")


def _turn_request(
    *, document_id: UUID, order: int, version_id: UUID | None = None
) -> DocumentBindingRequest:
    return DocumentBindingRequest(
        document_id=document_id,
        document_version_id=version_id,
        binding_role="current_turn_attachment",
        conversation_id="conversation-1",
        turn_id="turn-1",
        attachment_order=order,
    )


def test_current_turn_binding_is_idempotent_and_reloads_in_attachment_order() -> None:
    """FR-003: refresh/reconnect reloads one replay-safe ordered binding set."""

    store = InMemoryDocumentBindingStore()
    second = store.create(
        tenant_id=TENANT_A,
        actor_user_id=ACTOR_A,
        request=_turn_request(document_id=DOCUMENT_B, order=2),
        correlation_id="correlation-1",
    )
    first = store.create(
        tenant_id=TENANT_A,
        actor_user_id=ACTOR_A,
        request=_turn_request(document_id=DOCUMENT_A, order=1),
        correlation_id="correlation-1",
    )
    replay = store.create(
        tenant_id=TENANT_A,
        actor_user_id=ACTOR_A,
        request=_turn_request(document_id=DOCUMENT_A, order=1),
        correlation_id="correlation-2",
    )

    assert replay.document_binding_id == first.document_binding_id
    assert [
        binding.document_id
        for binding in store.list_for_target(
            tenant_id=TENANT_A,
            actor_user_id=ACTOR_A,
            conversation_id="conversation-1",
            turn_id="turn-1",
            workflow_id=None,
        )
    ] == [first.document_id, second.document_id]


def test_binding_targets_roles_and_tenant_reads_are_strictly_scoped() -> None:
    """FR-003: workflow and library bindings remain distinct and tenant-safe."""

    store = InMemoryDocumentBindingStore()
    workflow = DocumentBindingRequest(
        document_id=DOCUMENT_A,
        binding_role="workflow_reference",
        workflow_id="filing-1",
    )
    library = DocumentBindingRequest(
        document_id=DOCUMENT_A,
        binding_role="existing_library_document",
        conversation_id="conversation-1",
        turn_id="turn-1",
        attachment_order=0,
    )
    store.create(
        tenant_id=TENANT_A,
        actor_user_id=ACTOR_A,
        request=workflow,
        correlation_id="correlation-1",
    )
    store.create(
        tenant_id=TENANT_A,
        actor_user_id=ACTOR_A,
        request=library,
        correlation_id="correlation-1",
    )

    assert (
        len(
            store.list_for_target(
                tenant_id=TENANT_A,
                actor_user_id=ACTOR_A,
                conversation_id=None,
                turn_id=None,
                workflow_id="filing-1",
            )
        )
        == 1
    )
    assert not store.list_for_target(
        tenant_id=TENANT_A,
        actor_user_id=ACTOR_B,
        conversation_id="conversation-1",
        turn_id="turn-1",
        workflow_id=None,
    )
    assert not store.list_for_target(
        tenant_id="tenant-b",
        actor_user_id=ACTOR_A,
        conversation_id=None,
        turn_id=None,
        workflow_id="filing-1",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "document_id": str(DOCUMENT_A),
            "binding_role": "workflow_reference",
            "conversation_id": "c",
        },
        {
            "document_id": str(DOCUMENT_A),
            "binding_role": "current_turn_attachment",
            "conversation_id": "c",
            "turn_id": "t",
        },
        {
            "document_id": str(DOCUMENT_A),
            "binding_role": "conversation_attachment",
            "conversation_id": "c",
            "turn_id": "t",
        },
    ],
)
def test_invalid_binding_shapes_are_rejected(payload: dict[str, str]) -> None:
    """Current attachments cannot silently degrade into ambiguous target bindings."""

    with pytest.raises(ValidationError):
        DocumentBindingRequest.model_validate(payload)


def test_binding_migration_enforces_target_version_and_lifecycle_read_filters() -> None:
    """FR-003: PostgreSQL remains authoritative for version integrity and visibility."""

    migration = Path("database/migrations/0033_document_ai_durable_bindings.sql").read_text(
        encoding="utf-8"
    )
    repository = Path("services/document_ai/app/document_bindings.py").read_text(encoding="utf-8")
    assert "fn_document_ai_binding_version_same_document" in migration
    assert "uq_document_ai_document_bindings_logical_target" in migration
    assert "attachment_order >= 0" in migration
    assert "document.state IN ('uploaded', 'processing', 'validated')" in repository
    assert "COALESCE(binding.document_version_id," in repository
