"""Explicit deterministic resolver for tests that are not testing OpenAI transport."""

from __future__ import annotations

from services.orchestration.app.conversation_turn_resolution import (
    ConversationTurnResolution,
    ConversationTurnResolutionInput,
)
from services.orchestration.app.prompt_intent_envelope import (
    PromptIntentEnvelopeError,
    parse_income_tax_prompt_intent_envelope,
)


class DeterministicTestConversationTurnResolver:
    """Test double only; production never imports or selects this resolver."""

    def __init__(self) -> None:
        self.calls = 0

    def resolve_turn(self, payload: ConversationTurnResolutionInput) -> ConversationTurnResolution:
        self.calls += 1
        try:
            envelope = parse_income_tax_prompt_intent_envelope(
                payload.current_prompt,
                current_tax_year=2026,
            )
        except PromptIntentEnvelopeError:
            envelope = {
                "intent_class": "unknown",
                "tax_domain_hint": "unknown",
                "tax_year_hint": None,
            }

        intent = str(envelope["intent_class"])
        domain = str(envelope["tax_domain_hint"])
        clarification = intent == "clarification_required"
        prompt_lower = payload.current_prompt.lower()
        raw_reason_code = str(envelope.get("clarification_reason_code") or "")

        if "form and report" in prompt_lower:
            intent = "clarification_required"
            clarification = True
            domain = "income_tax"
            envelope = {
                **envelope,
                "clarification_reason_code": "ambiguous_service_family",
                "clarification_message": "Do you want a form or a report?",
                "candidate_service_families": ("forms", "reports"),
                "required_context_fields": ("service_family_choice",),
            }

        if "lookup statutory authority" in prompt_lower and "paye" in prompt_lower:
            intent = "lookup_grounded_knowledge"
            domain = "paye_generalized"
            clarification = False

        if (
            intent == "unsupported_domain_request"
            and domain == "unknown"
            and raw_reason_code
            and raw_reason_code != "ambiguous_tax_domain"
        ):
            intent = "clarification_required"
            clarification = True
            envelope = {
                **envelope,
                "clarification_reason_code": "no_prior_conversation_context",
                "clarification_message": "Please provide the prior conversation context you want me to use.",
                "required_context_fields": ("prior_conversation_context",),
            }

        regime_identifier = envelope.get("regime_identifier_hint")
        if domain == "health_contribution" and regime_identifier is None:
            historical_version_hint = str(envelope.get("historical_version_hint") or "")
            if "2003" in historical_version_hint or "2009" in prompt_lower:
                regime_identifier = "nhif_legacy"
            else:
                regime_identifier = "sha_shif"

        if domain == "unknown" and intent != "clarification_required":
            intent = "unknown"

        required_context_fields = list(envelope.get("required_context_fields", ()))
        missing_required_context_fields = list(envelope.get("required_context_fields", ()))
        if clarification and not required_context_fields:
            required_context_fields = ["required_context"]
            missing_required_context_fields = ["required_context"]
        if not clarification:
            required_context_fields = []
            missing_required_context_fields = []

        return ConversationTurnResolution.model_validate(
            {
                "schema_version": "1.0",
                "relationship": "standalone",
                "operation_mode": "computation" if intent.startswith("compute_") else "informational",
                "raw_prompt": payload.current_prompt,
                "contextualized_prompt": payload.current_prompt,
                "intent_class": intent,
                "tax_domain_hint": domain,
                "retrieval_tax_domain_filter": (
                    domain
                    if intent in {"lookup_grounded_knowledge", "retrieve_grounded_knowledge"}
                    and domain != "general_tax"
                    else None
                ),
                "jurisdiction_hint": "Kenya",
                "tax_year_hint": envelope.get("tax_year_hint"),
                "supported_lane_id": envelope.get("requested_lane_hint"),
                "historical_version_id": envelope.get("historical_version_hint"),
                "regime_identifier": regime_identifier,
                "answerability": "clarification_required" if clarification else "answerable",
                "clarification_reason_code": (
                    str(envelope.get("clarification_reason_code") or "missing_required_context")
                    if clarification
                    else None
                ),
                "clarification_question": (
                    str(envelope.get("clarification_message") or "Please provide the required information.")
                    if clarification
                    else None
                ),
                "candidate_service_families": list(envelope.get("candidate_service_families", ())),
                "required_context_fields": required_context_fields,
                "provided_context_fields": [],
                "missing_required_context_fields": missing_required_context_fields,
                "needs_knowledge_retrieval": intent in {"lookup_grounded_knowledge", "retrieve_grounded_knowledge"},
                "needs_computation": intent.startswith("compute_"),
                "needs_external_action": False,
                "needs_artifact_operation": intent in {
                    "generate_form_artifact",
                    "generate_report_artifact",
                    "extract_document",
                },
                "referenced_candidate_ids": [],
                "resolved_references": [],
                "retained_fields": [],
                "corrected_fields": [],
                "reuse_prior_semantic_facts": False,
                "reuse_prior_computation_result": False,
                "reuse_prior_evidence": False,
                "reuse_prior_artifact": False,
                "assumptions": [],
                "confidence": 1.0,
                "audit_summary": "deterministic test resolver",
            }
        )
