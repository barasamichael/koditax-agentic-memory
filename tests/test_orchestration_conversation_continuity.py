"""Conversation continuity checks for orchestration follow-up execution."""

from __future__ import annotations

import time
from datetime import date
from typing import Any, cast
from uuid import uuid4
from pathlib import Path

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from httpx import Response as HttpxResponse

from services.orchestration.app.conversation_state_protection import (
    LocalAesGcmConversationStateProtector,
)
from services.orchestration.app.conversation_state_store import InMemoryConversationStateStore
from services.orchestration.app.main import create_app
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from tests.orchestration_auth_support import orchestration_auth_headers


pytestmark = pytest.mark.integration


# pyright: reportUnusedFunction=false
class _KnowledgeOrchestrationStub:
    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (query, source_type, tax_domain, effective_date)
        return (
            KnowledgeSearchRecord(
                source_id="KNW-ITA-15-2",
                title="Income Tax Act (Cap. 470), Section 15(2)",
                url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
                source_type="tax_law",
                tax_domain="income_tax",
                authority_level="statute",
                effective_from="1974-01-01",
                effective_to=None,
                tax_year=None,
                anchor_id="income-tax-act-15-2",
                content="Allowable deductions in production of income under section 15(2).",
            ),
        )

    def retrieve_records(
        self,
        *,
        source_ids: tuple[str, ...],
        anchor_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (source_ids, anchor_ids)
        return ()

    def timeline_search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str,
        start_date: date,
        end_date: date,
    ) -> tuple[KnowledgeTimelineRecord, ...]:
        _ = (query, source_type, tax_domain, start_date, end_date)
        return ()

    def list_source_versions(
        self,
        *,
        publication_state: str | None,
        source_id: str | None,
        source_family_id: str | None,
        tax_domain: str | None,
        source_class: str | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[KnowledgeSourceVersionSummaryRecord, ...]:
        _ = (
            publication_state,
            source_id,
            source_family_id,
            tax_domain,
            source_class,
            limit,
            offset,
            sort_by,
            sort_order,
        )
        return (
            KnowledgeSourceVersionSummaryRecord(
                source_version_id="123e4567-e89b-12d3-a456-426614174100",
                source_id="KNW-ITA-15-2",
                source_family_id="KNW-ITA-FAMILY",
                title="Income Tax Act (Cap. 470), Section 15(2)",
                source_class="tax_law",
                tax_domain="income_tax",
                authority_level="statute",
                publication_state="published",
                source_input_origin="official_source_upload",
                source_version_form="point_in_time_consolidation",
                effective_from="1974-01-01",
                effective_to=None,
                tax_year=None,
                supersedes_source_version_id=None,
                superseded_by_source_version_id=None,
            ),
        )


# pyright: reportUnusedFunction=false
@pytest.fixture(autouse=True)
def _use_live_openai_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    load_dotenv(Path(".env"))
    monkeypatch.setenv("KODI_LIVE_OPENAI_TEST", "1")


def test_vat_followup_reuses_prior_context_and_executes() -> None:
    client = _live_client(user_reference=_unique_user_reference("vat-followup"))
    _decide_and_execute(
        client,
        conversation_id="conv-knowledge-followup-001",
        idempotency_key=_unique_idempotency_key("knowledge-initial"),
        prompt_text="lookup statutory authority for allowable deductions in income tax effective 2024-12-27.",
    )
    time.sleep(1.0)

    decide = _decide_with_retry(
        client,
        conversation_id="conv-knowledge-followup-001",
        prompt_text="Which acts govern it?",
    )

    assert decide.status_code == 200
    decision = cast(dict[str, Any], decide.json())
    assert decision["status"] == "resolved"
    assert decision["intent_class"] == "lookup_grounded_knowledge"
    assert decision["tax_domain_hint"] == "income_tax"
    assert decision["turn_resolution"]["relationship"] == "continuation"
    assert "allowable deductions" in decision["turn_resolution"]["contextualized_prompt"].lower()
    assert decision["selected_route"]["target_service"] == "knowledge"

    execute = _execute(
        client,
        conversation_id="conv-knowledge-followup-001",
        idempotency_key=_unique_idempotency_key("knowledge-followup"),
        decision=decision,
        prompt_text="Which acts govern it?",
    )

    assert execute.status_code == 200
    body = cast(dict[str, Any], execute.json())
    assert body["response"]["status"] == "generated"
    assert body["response"]["answer_mode"] == "grounded_knowledge"
    assert body["grounding_status"] == "grounded"


def test_broad_followup_shifts_to_general_tax_without_reusing_prior_tax_context() -> None:
    client = _live_client(user_reference=_unique_user_reference("fish-tax-followup"))
    _decide_and_execute(
        client,
        conversation_id="conv-fish-tax-followup-001",
        idempotency_key=_unique_idempotency_key("fish-tax-initial"),
        prompt_text="lookup statutory authority for allowable deductions in income tax effective 2024-12-27.",
    )

    decide = _decide_with_retry(
        client,
        conversation_id="conv-fish-tax-followup-001",
        prompt_text="What about fish tax?",
    )

    assert decide.status_code == 200
    decision = cast(dict[str, Any], decide.json())
    assert decision["status"] == "resolved"
    assert decision["intent_class"] == "lookup_grounded_knowledge"
    assert decision["tax_domain_hint"] == "general_tax"
    assert decision["turn_resolution"]["relationship"] in {"topic_shift", "standalone"}
    assert "fish" in decision["turn_resolution"]["contextualized_prompt"].lower()
    assert "vat" not in decision["turn_resolution"]["contextualized_prompt"].lower()

    execute = _execute(
        client,
        conversation_id="conv-fish-tax-followup-001",
        idempotency_key=_unique_idempotency_key("fish-tax-followup"),
        decision=decision,
        prompt_text="What about fish tax?",
    )

    assert execute.status_code == 200
    body = cast(dict[str, Any], execute.json())
    assert body["response"]["status"] == "generated"
    assert body["response"]["answer_mode"] == "grounded_knowledge"
    assert body["grounding_status"] == "grounded"


def test_married_filing_question_is_kept_separate_from_vat_context() -> None:
    client = _live_client(user_reference=_unique_user_reference("married-filing"))

    decide = _decide(
        client,
        conversation_id="conv-married-filing-001",
        prompt_text="My husband and I got married earlier this year. Should we file together?",
    )

    assert decide.status_code == 200
    decision = cast(dict[str, Any], decide.json())
    assert decision["status"] == "resolved"
    assert decision["intent_class"] == "lookup_grounded_knowledge"
    assert decision["tax_domain_hint"] == "income_tax"
    assert "married" in decision["turn_resolution"]["contextualized_prompt"].lower()
    assert "file" in decision["turn_resolution"]["contextualized_prompt"].lower()

    execute = _execute(
        client,
        conversation_id="conv-married-filing-001",
        idempotency_key=_unique_idempotency_key("married-filing"),
        decision=decision,
        prompt_text="My husband and I got married earlier this year. Should we file together?",
    )

    assert execute.status_code == 200
    body = cast(dict[str, Any], execute.json())
    assert body["response"]["status"] == "generated"
    assert body["response"]["answer_mode"] == "grounded_knowledge"
    assert body["grounding_status"] == "grounded"


def _decide(
    client: TestClient,
    *,
    conversation_id: str,
    prompt_text: str,
) -> HttpxResponse:
    return client.post(
        "/v1/orchestration/prompt/decide",
        headers={"X-Correlation-ID": f"corr-{conversation_id}-decide"},
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": conversation_id,
            "channel": "chat",
            "prompt": {
                "text": prompt_text,
                "format": "plain_text",
            },
        },
    )


def _execute(
    client: TestClient,
    *,
    conversation_id: str,
    idempotency_key: str,
    decision: dict[str, Any],
    prompt_text: str,
) -> HttpxResponse:
    return client.post(
        "/v1/orchestration/prompt/execute",
        headers={"X-Correlation-ID": f"corr-{conversation_id}-execute"},
        json={
            "tenant_id": "pilot_tenant_alpha",
            "conversation_id": conversation_id,
            "channel": "chat",
            "prompt": {
                "text": prompt_text,
                "format": "plain_text",
            },
            "idempotency_key": idempotency_key,
            "intent_class": decision["intent_class"],
            "tax_domain_hint": decision["tax_domain_hint"],
            "decision_id": decision["decision_id"],
            "selected_route": decision["selected_route"],
        },
    )


def _decide_and_execute(
    client: TestClient,
    *,
    conversation_id: str,
    idempotency_key: str,
    prompt_text: str,
) -> None:
    decision_response = _decide(client, conversation_id=conversation_id, prompt_text=prompt_text)
    assert decision_response.status_code == 200
    decision = cast(dict[str, Any], decision_response.json())
    execute_response = _execute(
        client,
        conversation_id=conversation_id,
        idempotency_key=idempotency_key,
        decision=decision,
        prompt_text=prompt_text,
    )
    assert execute_response.status_code == 200


def _decide_with_retry(
    client: TestClient,
    *,
    conversation_id: str,
    prompt_text: str,
    attempts: int = 5,
    delay_seconds: float = 1.0,
) -> HttpxResponse:
    last_response: HttpxResponse | None = None
    for attempt in range(attempts):
        last_response = _decide(client, conversation_id=conversation_id, prompt_text=prompt_text)
        if last_response.status_code != 500:
            return last_response
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    assert last_response is not None
    return last_response


def _live_client(*, user_reference: str) -> TestClient:
    app = create_app(
        conversation_state_store=InMemoryConversationStateStore(),
        conversation_state_protector=_conversation_state_protector(),
        knowledge_repository=_KnowledgeOrchestrationStub(),
    )
    return TestClient(app, headers=orchestration_auth_headers(user_reference=user_reference))


def _conversation_state_protector() -> LocalAesGcmConversationStateProtector:
    return LocalAesGcmConversationStateProtector(key=b"a" * 32)


def _unique_idempotency_key(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _unique_user_reference(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"
