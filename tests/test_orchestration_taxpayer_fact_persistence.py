"""Focused coverage for explicit taxpayer-fact extraction and persistence."""

from __future__ import annotations

from concurrent.futures import Future
from typing import Literal
from typing import cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from services.orchestration.app.main import create_app
from tests.orchestration_auth_support import orchestration_auth_headers
from tests.orchestration_auth_support import orchestration_test_user_id
import services.orchestration.app.prompt_intent_envelope as prompt_intent_envelope
from services.orchestration.app.conversation_state_store import InMemoryConversationStateStore
from services.orchestration.app.prompt_semantic_extractor import ExtractedSemanticContext
from services.orchestration.app.prompt_semantic_extractor import ExtractedTaxpayerFacts
from services.orchestration.app.conversation_state_protection import protect_stated_facts
from services.orchestration.app.conversation_state_protection import (
    ConversationStateProtectionError,
)
from services.orchestration.app.conversation_state_protection import (
    LocalAesGcmConversationStateProtector,
)


def test_explicit_turnover_is_persisted_for_execution() -> None:
    store = InMemoryConversationStateStore()
    app = create_app(
        conversation_state_store=store,
        conversation_state_protector=LocalAesGcmConversationStateProtector(key=b"a" * 32),
    )
    client = TestClient(app, headers=orchestration_auth_headers(user_reference="stated-facts"))

    original_launch = getattr(prompt_intent_envelope, "_launch_semantic_extractor")

    def launch_extractor(**_: object) -> Future[ExtractedSemanticContext]:
        future: Future[ExtractedSemanticContext] = Future()
        future.set_result(_extracted_context(turnover_amount_kes=4200000, turnover_confidence=0.98))
        return future

    setattr(prompt_intent_envelope, "_launch_semantic_extractor", launch_extractor)
    try:
        prompt = (
            "My turnover is KES 4.2 million. Compute income tax for resident employment "
            "lane in tax year 2023 under KIT-VER-20230701-A."
        )
        decide = client.post(
            "/v1/orchestration/prompt/decide",
            headers={"X-Correlation-ID": "corr-stated-facts-decide-001"},
            json={
                "tenant_id": "pilot_tenant_alpha",
                "conversation_id": "conv-stated-facts-001",
                "channel": "chat",
                "prompt": {"text": prompt, "format": "plain_text"},
            },
        )
        assert decide.status_code == 200
        decision = decide.json()

        execute = client.post(
            "/v1/orchestration/prompt/execute",
            headers={"X-Correlation-ID": "corr-stated-facts-execute-001"},
            json={
                "tenant_id": "pilot_tenant_alpha",
                "conversation_id": "conv-stated-facts-001",
                "channel": "chat",
                "prompt": {"text": prompt, "format": "plain_text"},
                "idempotency_key": f"idem-stated-facts-{uuid4().hex}",
                "intent_class": decision["intent_class"],
                "tax_domain_hint": decision["tax_domain_hint"],
                "decision_id": decision["decision_id"],
                "selected_route": decision["selected_route"],
            },
        )
        assert execute.status_code == 409
    finally:
        setattr(prompt_intent_envelope, "_launch_semantic_extractor", original_launch)

    execute_detail = cast(dict[str, object], execute.json()["detail"])
    assert execute_detail["error_code"] == "clarification_required"
    assert "turnover" in str(execute_detail["message"]).lower()
    assert store.list_recent(
        tenant_id="pilot_tenant_alpha",
        conversation_id="conv-stated-facts-001",
        user_id=orchestration_test_user_id("stated-facts"),
        limit=10,
    ) == ()


def test_turnover_is_not_inferred_from_an_explicit_income_statement() -> None:
    extracted = _parse(
        {
            "income_amount_kes": 120000,
            "income_frequency": "monthly",
            "turnover_amount_kes": None,
            "residency_status": None,
            "filing_status": None,
            "confidence_per_field": {
                "income_amount_kes": 0.95,
                "income_frequency": 0.95,
                "turnover_amount_kes": 0.0,
                "residency_status": 0.0,
                "filing_status": 0.0,
            },
        }
    )

    stated_facts = cast(dict[str, object], extracted["stated_facts"])
    assert stated_facts["income_amount_kes"] == 120000.0
    assert stated_facts["turnover_amount_kes"] is None


def test_low_confidence_taxpayer_fact_is_not_persistable() -> None:
    extracted = _parse(
        {
            "income_amount_kes": None,
            "income_frequency": None,
            "turnover_amount_kes": 4200000,
            "residency_status": None,
            "filing_status": None,
            "confidence_per_field": {
                "income_amount_kes": 0.0,
                "income_frequency": 0.0,
                "turnover_amount_kes": 0.69,
                "residency_status": 0.0,
                "filing_status": 0.0,
            },
        }
    )

    stated_facts = cast(dict[str, object], extracted["stated_facts"])
    confidence_per_field = cast(dict[str, object], stated_facts["confidence_per_field"])
    assert stated_facts["turnover_amount_kes"] is None
    assert confidence_per_field["turnover_amount_kes"] == 0.69


def test_sensitive_facts_fail_closed_without_configured_aes_protection() -> None:
    with pytest.raises(ConversationStateProtectionError):
        protect_stated_facts(
            stated_facts={
                "turnover_amount_kes": 4200000.0,
                "confidence_per_field": {"turnover_amount_kes": 0.98},
            },
            protector=None,
        )


def _parse(stated_facts: dict[str, object]) -> ExtractedSemanticContext:
    normalized_stated_facts = _normalize_stated_facts(stated_facts)
    return {
        "tax_year": 2023,
        "regime": "resident employment",
        "intent_class": "compute_income_tax",
        "tax_domain_hint": "income_tax",
        "confidence": 0.95,
        "inferred_fields": [],
        "implicit_context": {},
        "extraction_status": "extracted",
        "is_tax_related": True,
        "requires_computation": True,
        "stated_facts": normalized_stated_facts,
    }


def _extracted_context(
    *,
    turnover_amount_kes: float,
    turnover_confidence: float,
) -> ExtractedSemanticContext:
    return _parse(
        {
            "income_amount_kes": None,
            "income_frequency": None,
            "turnover_amount_kes": turnover_amount_kes,
            "residency_status": None,
            "filing_status": None,
            "confidence_per_field": {
                "income_amount_kes": 0.0,
                "income_frequency": 0.0,
                "turnover_amount_kes": turnover_confidence,
                "residency_status": 0.0,
                "filing_status": 0.0,
            },
        }
    )


def _normalize_stated_facts(stated_facts: dict[str, object]) -> ExtractedTaxpayerFacts:
    confidence = cast(dict[str, float], stated_facts.get("confidence_per_field", {}))
    normalized: ExtractedTaxpayerFacts = {
        "income_amount_kes": None,
        "income_frequency": None,
        "turnover_amount_kes": None,
        "residency_status": None,
        "filing_status": None,
        "confidence_per_field": {
            "income_amount_kes": float(confidence.get("income_amount_kes", 0.0)),
            "income_frequency": float(confidence.get("income_frequency", 0.0)),
            "turnover_amount_kes": float(confidence.get("turnover_amount_kes", 0.0)),
            "residency_status": float(confidence.get("residency_status", 0.0)),
            "filing_status": float(confidence.get("filing_status", 0.0)),
        },
    }
    if normalized["confidence_per_field"]["income_amount_kes"] >= 0.7:
        value = stated_facts.get("income_amount_kes")
        normalized["income_amount_kes"] = float(value) if isinstance(value, (int, float)) else None
    if normalized["confidence_per_field"]["income_frequency"] >= 0.7:
        value = stated_facts.get("income_frequency")
        if value in {"monthly", "annual"}:
            normalized["income_frequency"] = cast(
                Literal["monthly", "annual"], value
            )
    if normalized["confidence_per_field"]["turnover_amount_kes"] >= 0.7:
        value = stated_facts.get("turnover_amount_kes")
        normalized["turnover_amount_kes"] = float(value) if isinstance(value, (int, float)) else None
    if normalized["confidence_per_field"]["residency_status"] >= 0.7:
        value = stated_facts.get("residency_status")
        if value in {"resident", "non_resident"}:
            normalized["residency_status"] = cast(
                Literal["resident", "non_resident"], value
            )
    if normalized["confidence_per_field"]["filing_status"] >= 0.7:
        value = stated_facts.get("filing_status")
        normalized["filing_status"] = str(value) if isinstance(value, str) and value.strip() else None
    return normalized
