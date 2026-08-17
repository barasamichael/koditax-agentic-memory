from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import load_dotenv

from services.orchestration.app.config import load_orchestration_openai_response_synthesis_config
from services.orchestration.app.conversation_turn_resolution import (
    ConversationTurnResolutionInput,
    OpenAIConversationTurnResolver,
)


def _payload() -> ConversationTurnResolutionInput:
    return ConversationTurnResolutionInput.model_validate(
        {
            "today": "2026-07-28",
            "trusted_jurisdiction": "Kenya",
            "tenant_product_context": {
                "tenant_id": "pilot_tenant_alpha",
                "conversation_id": "conv-live-openai-test-001",
                "effective_taxpayer_user_id": "user_live_openai_test_001",
            },
            "current_prompt": "What is VAT?",
            "recent_candidates": [],
            "supported_intents": ["lookup_grounded_knowledge"],
            "supported_knowledge_domains": ["vat", "general_tax"],
            "supported_computations": [],
            "supported_artifact_operations": [],
            "external_action_considered": False,
            "immediately_preceding_clarification": None,
            "prior_failure_metadata": None,
        }
    )


@pytest.mark.integration
def test_openai_turn_resolver_queries_live_openai_and_returns_valid_resolution() -> None:
    load_dotenv(Path(".env"))
    cfg = load_orchestration_openai_response_synthesis_config()
    if not cfg.configured:
        pytest.skip("OpenAI resolver is not configured in the environment.")

    resolver = OpenAIConversationTurnResolver(
        client=__import__("openai").OpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout_seconds,
        ),
        model=cfg.model,
    )

    result = resolver.resolve_turn(_payload())

    assert result.schema_version == "1.0"
    assert result.intent_class == "lookup_grounded_knowledge"
    assert result.tax_domain_hint == "vat"
    assert result.contextualized_prompt
    assert "VAT" in result.contextualized_prompt
