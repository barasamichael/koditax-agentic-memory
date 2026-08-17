"""Conversation deletion tests for orchestration."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app
from tests.orchestration_auth_support import orchestration_auth_headers
from tests.orchestration_auth_support import orchestration_test_user_id
from services.orchestration.app.conversation_state_store import InMemoryConversationStateStore


def _conversation_state_record(
    *,
    execution_id: str,
    tenant_id: str,
    conversation_id: str,
    user_id: str,
) -> dict[str, object]:
    return {
        "execution_id": execution_id,
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "context_payload": {
            "prompt_text": "What is VAT?",
            "answer_summary": "VAT answer",
            "intent_class": "lookup_grounded_knowledge",
            "tax_domain_hint": "vat",
        },
    }


def test_single_conversation_delete_clears_only_the_scoped_owner_records() -> None:
    store = InMemoryConversationStateStore()
    current_user_reference = "delete-single-owner"
    current_user = orchestration_test_user_id(current_user_reference)
    other_user = str(uuid4())
    conversation_id = "conv-delete-single-001"
    store.put(
        _conversation_state_record(
            execution_id="exec-current-1",
            tenant_id="pilot_tenant_alpha",
            conversation_id=conversation_id,
            user_id=current_user,
        )
    )
    store.put(
        _conversation_state_record(
            execution_id="exec-other-1",
            tenant_id="pilot_tenant_beta",
            conversation_id=conversation_id,
            user_id=other_user,
        )
    )

    client = TestClient(create_app(conversation_state_store=store))
    response = client.delete(
        f"/v1/orchestration/conversations/{conversation_id}",
        headers=orchestration_auth_headers(user_reference=current_user_reference),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "deleted"
    assert payload["service"] == "orchestration"
    assert payload["conversation_id"] == conversation_id
    assert payload["deleted_count"] == 1
    assert store.list_recent(
        tenant_id="pilot_tenant_alpha",
        conversation_id=conversation_id,
        user_id=current_user,
        limit=10,
    ) == ()
    assert store.list_recent(
        tenant_id="pilot_tenant_beta",
        conversation_id=conversation_id,
        user_id=other_user,
        limit=10,
    ) != ()


def test_single_conversation_delete_is_idempotent() -> None:
    store = InMemoryConversationStateStore()
    conversation_id = "conv-delete-idempotent-001"
    user_reference = "delete-idempotent-owner"
    user_id = orchestration_test_user_id(user_reference)
    store.put(
        _conversation_state_record(
            execution_id="exec-idempotent-1",
            tenant_id="pilot_tenant_alpha",
            conversation_id=conversation_id,
            user_id=user_id,
        )
    )
    client = TestClient(create_app(conversation_state_store=store))
    headers = orchestration_auth_headers(user_reference=user_reference)

    first = client.delete(f"/v1/orchestration/conversations/{conversation_id}", headers=headers)
    second = client.delete(f"/v1/orchestration/conversations/{conversation_id}", headers=headers)

    assert first.status_code == 200
    assert first.json()["deleted_count"] == 1
    assert second.status_code == 200
    assert second.json()["deleted_count"] == 0


def test_bulk_conversation_delete_removes_authorized_targets_and_keeps_other_tenants() -> None:
    store = InMemoryConversationStateStore()
    user_reference = "bulk-delete-owner"
    current_user = orchestration_test_user_id(user_reference)
    other_user = str(uuid4())
    current_conversations = [
        "conv-bulk-delete-001",
        "conv-bulk-delete-002",
    ]
    for index, conversation_id in enumerate(current_conversations, start=1):
        store.put(
            _conversation_state_record(
                execution_id=f"exec-bulk-current-{index}",
                tenant_id="pilot_tenant_alpha",
                conversation_id=conversation_id,
                user_id=current_user,
            )
        )
    store.put(
        _conversation_state_record(
            execution_id="exec-bulk-other-1",
            tenant_id="pilot_tenant_beta",
            conversation_id=current_conversations[0],
            user_id=other_user,
        )
    )

    client = TestClient(create_app(conversation_state_store=store))
    response = client.post(
        "/v1/orchestration/conversations/bulk-delete",
        headers=orchestration_auth_headers(user_reference=user_reference),
        json={
            "conversation_ids": [
                current_conversations[0],
                "conv-bulk-delete-missing-001",
                current_conversations[1],
                current_conversations[0],
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "deleted"
    assert payload["requested_conversation_ids"] == [
        current_conversations[0],
        "conv-bulk-delete-missing-001",
        current_conversations[1],
    ]
    assert payload["deleted_conversation_ids"] == current_conversations
    assert payload["deleted_count"] == 2
    assert store.list_recent(
        tenant_id="pilot_tenant_alpha",
        conversation_id=current_conversations[0],
        user_id=current_user,
        limit=10,
    ) == ()
    assert store.list_recent(
        tenant_id="pilot_tenant_alpha",
        conversation_id=current_conversations[1],
        user_id=current_user,
        limit=10,
    ) == ()
    assert store.list_recent(
        tenant_id="pilot_tenant_beta",
        conversation_id=current_conversations[0],
        user_id=other_user,
        limit=10,
    ) != ()


def test_bulk_conversation_delete_rejects_missing_auth_context() -> None:
    client = TestClient(create_app(conversation_state_store=InMemoryConversationStateStore()))
    response = client.post(
        "/v1/orchestration/conversations/bulk-delete",
        headers={"X-Test-Anonymous": "1"},
        json={"conversation_ids": ["conv-bulk-delete-401"]},
    )

    assert response.status_code == 401
    detail = response.json()["detail"]
    assert detail["reason"] == "auth_context_missing"
    assert detail["reason_code"] == "auth_context_missing"
