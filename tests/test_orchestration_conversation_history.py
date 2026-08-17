"""Conversation history hydration tests for orchestration."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app
from tests.orchestration_auth_support import orchestration_auth_headers
from tests.orchestration_auth_support import orchestration_test_user_id
from services.orchestration.app.conversation_state_store import ConversationStateRecord
from services.orchestration.app.conversation_state_store import serialize_database_timestamp
from services.orchestration.app.conversation_state_store import InMemoryConversationStateStore


def _conversation_state_record(
    *,
    execution_id: str,
    tenant_id: str,
    conversation_id: str,
    user_id: str,
    prompt_text: str,
    answer_text: str,
    conversation_title: str,
) -> ConversationStateRecord:
    return {
        "execution_id": execution_id,
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "context_payload": {
            "raw_prompt_text": prompt_text,
            "assistant_answer_text": answer_text,
            "assistant_answer_summary": answer_text[:40],
            "conversation_title": conversation_title,
            "intent_class": "lookup_grounded_knowledge",
            "tax_domain_hint": "vat",
        },
        "created_at": "2026-08-02T10:00:00+00:00",
        "updated_at": "2026-08-02T10:00:01+00:00",
    }


def test_list_conversations_rehydrates_full_transcript_from_persisted_state() -> None:
    store = InMemoryConversationStateStore()
    user_reference = "history-owner"
    tenant_id = "pilot_tenant_alpha"
    scoped_user_id = orchestration_test_user_id(user_reference)
    conversation_id = "conv-history-001"
    store.put(
        _conversation_state_record(
            execution_id="exec-history-1",
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            user_id=scoped_user_id,
            prompt_text="What is VAT?",
            answer_text=(
                "VAT in Kenya is a consumption tax imposed on the value added to goods and "
                "services."
            ),
            conversation_title="VAT chat",
        )
    )

    client = TestClient(create_app(conversation_state_store=store))
    response = client.get(
        "/v1/orchestration/conversations",
        headers=orchestration_auth_headers(user_reference=user_reference),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "listed"
    assert payload["service"] == "orchestration"
    assert payload["conversations"][0]["conversation_id"] == conversation_id
    assert payload["conversations"][0]["created_at"] == "2026-08-02T10:00:00+00:00"
    assert payload["conversations"][0]["updated_at"] == "2026-08-02T10:00:00+00:00"
    assert payload["conversations"][0]["title"] == "VAT chat"
    assert payload["conversations"][0]["messages"][0]["content"] == "What is VAT?"
    assert payload["conversations"][0]["messages"][1]["content"].startswith(
        "VAT in Kenya is a consumption tax"
    )


def test_persistent_timestamp_is_serialized_for_browser_history() -> None:
    timestamp = datetime(2026, 8, 2, 10, 0, 1, tzinfo=UTC)

    assert serialize_database_timestamp(timestamp) == "2026-08-02T10:00:01+00:00"
