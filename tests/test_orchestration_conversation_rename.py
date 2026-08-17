"""Conversation rename tests for orchestration."""

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


def test_single_conversation_rename_updates_only_the_scoped_owner_records() -> None:
    store = InMemoryConversationStateStore()
    current_user_reference = "rename-single-owner"
    current_user = orchestration_test_user_id(current_user_reference)
    other_user = str(uuid4())
    conversation_id = "conv-rename-single-001"
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
    response = client.patch(
        f"/v1/orchestration/conversations/{conversation_id}",
        headers=orchestration_auth_headers(user_reference=current_user_reference),
        json={"conversation_title": "Renamed VAT chat"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "renamed"
    assert payload["service"] == "orchestration"
    assert payload["conversation_id"] == conversation_id
    assert payload["conversation_title"] == "Renamed VAT chat"
    assert payload["updated_count"] == 1
    assert store.list_recent(
        tenant_id="pilot_tenant_alpha",
        conversation_id=conversation_id,
        user_id=current_user,
        limit=10,
    )[0]["context_payload"]["conversation_title"] == "Renamed VAT chat"
    assert store.list_recent(
        tenant_id="pilot_tenant_beta",
        conversation_id=conversation_id,
        user_id=other_user,
        limit=10,
    )[0]["context_payload"].get("conversation_title") is None


def test_single_conversation_rename_is_idempotent() -> None:
    store = InMemoryConversationStateStore()
    conversation_id = "conv-rename-idempotent-001"
    user_reference = "rename-idempotent-owner"
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

    first = client.patch(
        f"/v1/orchestration/conversations/{conversation_id}",
        headers=headers,
        json={"conversation_title": "Renamed once"},
    )
    second = client.patch(
        f"/v1/orchestration/conversations/{conversation_id}",
        headers=headers,
        json={"conversation_title": "Renamed once"},
    )

    assert first.status_code == 200
    assert first.json()["updated_count"] == 1
    assert second.status_code == 200
    assert second.json()["updated_count"] == 0


def test_conversation_rename_rejects_missing_auth_context() -> None:
    client = TestClient(create_app(conversation_state_store=InMemoryConversationStateStore()))
    response = client.patch(
        "/v1/orchestration/conversations/conv-rename-401",
        headers={"X-Test-Anonymous": "1"},
        json={"conversation_title": "Renamed"},
    )

    assert response.status_code == 401
    detail = response.json()["detail"]
    assert detail["reason"] == "auth_context_missing"
    assert detail["reason_code"] == "auth_context_missing"


def test_conversation_rename_rejects_empty_title() -> None:
    client = TestClient(create_app(conversation_state_store=InMemoryConversationStateStore()))
    response = client.patch(
        "/v1/orchestration/conversations/conv-rename-empty",
        headers=orchestration_auth_headers(user_reference="rename-empty-owner"),
        json={"conversation_title": "   "},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error_code"] == "invalid_orchestration_request"
    assert detail["reason_code"] == "invalid_orchestration_request"
