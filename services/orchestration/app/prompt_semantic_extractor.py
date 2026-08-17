"""LLM-powered semantic context extraction for prompt intent determination."""

from __future__ import annotations

import json
import datetime
import math
import re
from typing import cast
from typing import Literal
from typing import NotRequired
from typing import TypedDict

from openai import OpenAI

from services.orchestration.app.config import FollowupClassificationConfig
from services.orchestration.app.config import SemanticPromptExtractionConfig
from services.orchestration.app.config import load_followup_classification_config
from services.orchestration.app.config import load_semantic_prompt_extraction_config
from services.orchestration.app.semantic_request_adjudicator import SemanticTaxpayerFacts
from services.orchestration.app.semantic_request_adjudicator import (
    SemanticRequestAdjudication,
)
from services.orchestration.app.semantic_request_adjudicator import (
    SemanticRequestAdjudicator,
)
from services.orchestration.app.semantic_request_adjudicator import (
    SemanticRequestAdjudicatorError,
)
from services.orchestration.app.conversation_state_transition import (
    ConversationStateTransitionAdjudicator,
)
from services.orchestration.app.conversation_state_transition import (
    ConversationStateTransitionProposal,
)
from services.orchestration.app.synthesis_integrity_constants import FACT_EXTRACTION_MIN_CONFIDENCE
from services.orchestration.app.request_timer import timed_print


class SemanticExtractionError(RuntimeError):
    """Represent semantic context extraction failures."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        reason_code: str,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.reason_code = reason_code
        self.context = context


class ExtractedSemanticContext(TypedDict):
    """Represent semantically extracted context from a prompt."""

    tax_year: int | None
    regime: str | None
    intent_class: str
    tax_domain_hint: str
    confidence: float
    inferred_fields: list[str]
    implicit_context: dict[str, object]
    extraction_status: str
    is_tax_related: bool
    requires_computation: bool
    stated_facts: ExtractedTaxpayerFacts
    semantic_frame: NotRequired[dict[str, object]]


class ExtractedTaxpayerFacts(TypedDict, total=False):
    """Represent taxpayer facts explicitly stated in one prompt."""

    income_amount_kes: float | None
    income_frequency: Literal["monthly", "annual"] | None
    turnover_amount_kes: float | None
    residency_status: Literal["resident", "non_resident"] | None
    filing_status: str | None
    confidence_per_field: dict[str, float]


class UserContextSummary(TypedDict):
    """Represent available user profile context for semantic extraction."""

    user_id: str
    tenant_id: str
    employment_type: str | None
    filing_status: str | None
    country: str | None
    jurisdiction: str | None


class ClassificationSignal(TypedDict):
    """Represent a single classification signal from one source (rule or LLM)."""

    source: Literal["rule", "llm", "ensemble"]
    intent_class: str
    tax_domain_hint: str
    confidence: float
    tax_year: int | None
    regime: str | None
    inferred_fields: list[str]


class PromptSemanticExtractor:
    """Use OpenAI to semantically extract tax context from prompts."""

    def __init__(self, *, config: SemanticPromptExtractionConfig | None = None) -> None:
        self._config = config or load_semantic_prompt_extraction_config()
        if self._config.configured:
            self._client = OpenAI(
                api_key=self._config.api_key,
                base_url=self._config.base_url,
                timeout=self._config.timeout_seconds,
            )
        else:
            self._client = None

    @property
    def is_configured(self) -> bool:
        """Return whether this extractor has valid LLM credentials and is enabled."""
        return self._config.configured

    def extract(
        self,
        prompt_text: str,
        user_context: UserContextSummary | None = None,
        conversation_history: list[str] | None = None,
        current_tax_year: int | None = None,
    ) -> ExtractedSemanticContext:
        """Extract semantic context using the structured adjudicator."""

        adjudicator = SemanticRequestAdjudicator(config=self._config, client=self._client)
        try:
            timed_print("[TURN_RESOLVER] About to adjudicate semantic extraction")
            adjudication = adjudicator.adjudicate(
                prompt_text=prompt_text,
                user_context=user_context,
                conversation_history=conversation_history,
                current_tax_year=current_tax_year,
            )
            timed_print(
                "[TURN_RESOLVER] Adjudicated semantic extraction "
                f"confidence={adjudication.confidence_score}"
            )
        except SemanticRequestAdjudicatorError as error:
            timed_print("[TURN_RESOLVER] Semantic extraction adjudication failed")
            raise SemanticExtractionError(
                error_code=error.error_code,
                message=error.message,
                reason_code=error.reason_code,
                context=error.context,
            ) from error

        return _semantic_adjudication_to_extracted_context(adjudication)

    def _build_extraction_prompt(
        self,
        prompt_text: str,
        user_context: UserContextSummary | None = None,
        conversation_history: list[str] | None = None,
        current_tax_year: int | None = None,
    ) -> str:
        """Build compact extraction prompt. Every token here costs latency."""

        today = datetime.date.today()
        inferred_current_tax_year = current_tax_year if current_tax_year is not None else today.year

        ctx_parts: list[str] = [
            f"Today: {today.isoformat()}. Current tax year: {inferred_current_tax_year}."
        ]
        if user_context:
            ctx_parts.append(
                f"User: employment_type={user_context.get('employment_type')}, "
                f"country={user_context.get('country')}."
            )
        if conversation_history:
            ctx_parts.append("Prior: " + " | ".join(conversation_history[-2:]))

        context_block = " ".join(ctx_parts)

        return (
            f"{context_block}\n"
            f'Prompt: "{prompt_text}"\n\n'
            "Classify the prompt. Return JSON:\n"
            "{\n"
            '  "is_tax_related": bool,        // true if about Kenyan tax; false only for sports/recipes/medicine/travel/code\n'
            '  "requires_computation": bool,   // true when the user asks to compute or calculate a tax result\n'
            '  "tax_year": int,                // year mentioned, or infer current tax year — never null\n'
            '  "regime": str|null,             // employment type or null\n'
            '  "intent_class": str,            // one of: compute_income_tax, compute_health_contribution, compute_plus_grounding, lookup_grounded_knowledge, retrieve_grounded_knowledge, generate_form, generate_report, extract_document, unknown\n'
            '  "tax_domain_hint": str,         // one of: income_tax, health_contribution, paye_generalized, vat, withholding_tax_generalized, business_income_generalized, rental_income_generalized, unknown\n'
            '  "confidence": float,            // 0.0–1.0\n'
            '  "inferred_fields": [],          // list of field names inferred rather than explicit\n'
            '  "implicit_context": {},         // any implied context\n'
            '  "stated_facts": {\n'
            '    "income_amount_kes": number|null,\n'
            '    "income_frequency": "monthly"|"annual"|null,\n'
            '    "turnover_amount_kes": number|null,\n'
            '    "residency_status": "resident"|"non_resident"|null,\n'
            '    "filing_status": string|null,\n'
            '    "confidence_per_field": {"income_amount_kes": number, "income_frequency": number, "turnover_amount_kes": number, "residency_status": number, "filing_status": number}\n'
            "  }\n"
            "}\n\n"
            "Rules (apply in order):\n"
            "1. is_tax_related=false ONLY for zero-tax topics (sports, recipes, medicine, travel, code). Tax cross-domain questions (e.g. 'does VAT apply to salaries?') are IN-SCOPE → true.\n"
            "2. is_tax_related=false for personal queries about KRA as an employer/institution (e.g. 'does my father work at KRA?', 'how do I get a job at KRA?', 'what is the KRA office address?', 'who is the KRA Commissioner General?', 'how many employees does KRA have?'). KRA is IN-SCOPE only when the question is about a tax law, tax obligation, tax procedure, tax rate, or tax compliance matter that KRA administers.\n"
            "3. is_tax_related=false for questions about KRA or tax as a news/political/institutional subject — e.g. 'has KRA met its revenue target?', 'did KRA win the court case against Safaricom?', 'why is Kenya tax revenue declining?', 'what is KRA annual budget?'. These are journalism/economics questions, not tax advice.\n"
            "4. is_tax_related=false when the tax topic is explicitly about a non-Kenyan jurisdiction and the user is not asking how it relates to Kenya (e.g. 'how does VAT work in the UK?', 'what is the US capital gains tax rate?', 'how does PAYE work in Tanzania?'). IN-SCOPE if the user asks how a foreign rule compares to or affects a Kenya-based situation.\n"
            "5. requires_computation=true when the user asks to compute/calculate a tax result, even if governed fixture or input details are still needed (e.g. 'compute income tax for resident employment lane' or 'calculate PAYE on KES 80,000'). Rate questions, bracket questions, and 'how much is VAT' = false.\n"
            "6. tax_year: extract explicit year integer. If none stated, return current tax year. Never null.\n"
            "7. intent_class: lookup_grounded_knowledge for rates/rules/explanations/policy. Use compute_* for an explicit calculation request; use compute_plus_grounding when a calculation request also asks for legal basis, statutory authority, or cited grounding. Downstream governed validation handles missing inputs.\n"
            "8. tax_domain_hint: match the primary tax topic. Unknown only when genuinely ambiguous.\n"
            "9. stated_facts: populate a field only when the prompt explicitly states it. "
            "Never infer turnover from income, residency from a location or employer, "
            "or any fact from prior context or world knowledge. Use null when a fact is "
            "not explicit. confidence_per_field must give each fact field a 0.0-1.0 confidence.\n"
        )


def _semantic_adjudication_to_extracted_context(
    adjudication: SemanticRequestAdjudication,
) -> ExtractedSemanticContext:
    semantic_frame = adjudication.model_dump(mode="python")
    stated_facts = _extract_taxpayer_facts_from_semantic_frame(
        adjudication.taxpayer_facts,
    )
    inferred_fields: list[str] = []
    taxpayer_fact_payload = cast(
        dict[str, object],
        adjudication.taxpayer_facts.model_dump(mode="python"),
    )
    for field_name, observation in taxpayer_fact_payload.items():
        if isinstance(observation, dict) and cast(dict[str, object], observation).get("status") == "inferred":
            inferred_fields.append(field_name)
    if adjudication.temporal_scope.explicit_tax_year is not None and "tax_year" not in inferred_fields:
        inferred_fields.append("tax_year")

    return ExtractedSemanticContext(
        tax_year=adjudication.temporal_scope.explicit_tax_year,
        regime=_derive_regime_identifier(adjudication.taxpayer_facts),
        intent_class=_legacy_intent_class_from_adjudication(adjudication),
        tax_domain_hint=adjudication.primary_tax_domain,
        confidence=adjudication.confidence_score if adjudication.confidence_score is not None else _confidence_from_band(adjudication.confidence_band),
        inferred_fields=inferred_fields,
        implicit_context={
            "semantic_rationale": adjudication.semantic_rationale,
            "semantic_frame": semantic_frame,
            "clarification_required": adjudication.clarification_required,
            "abstained": adjudication.abstained,
        },
        extraction_status="extracted",
        is_tax_related=adjudication.primary_goal != "unsupported_or_off_topic",
        requires_computation="computation" in adjudication.requested_capabilities,
        stated_facts=stated_facts,
        semantic_frame=semantic_frame,
    )


def _extract_taxpayer_facts_from_semantic_frame(
    taxpayer_facts: SemanticTaxpayerFacts,
) -> ExtractedTaxpayerFacts:
    facts: ExtractedTaxpayerFacts = {
        "income_amount_kes": _semantic_coerce_amount(taxpayer_facts.income_amount_kes.value),
        "income_frequency": _semantic_coerce_frequency(taxpayer_facts.income_frequency.value),
        "turnover_amount_kes": _semantic_coerce_amount(taxpayer_facts.turnover_amount_kes.value),
        "residency_status": _semantic_coerce_residency(taxpayer_facts.residency_status.value),
        "filing_status": _semantic_coerce_filing_status(taxpayer_facts.filing_status.value),
        "confidence_per_field": {
            "income_amount_kes": taxpayer_facts.income_amount_kes.confidence,
            "income_frequency": taxpayer_facts.income_frequency.confidence,
            "turnover_amount_kes": taxpayer_facts.turnover_amount_kes.confidence,
            "residency_status": taxpayer_facts.residency_status.confidence,
            "filing_status": taxpayer_facts.filing_status.confidence,
        },
    }
    return facts


def _legacy_intent_class_from_adjudication(
    adjudication: SemanticRequestAdjudication,
) -> str:
    if adjudication.adjudication_status == "clarification_required":
        return "clarification_required"
    mapping = {
        "compute_tax_obligation": "compute_income_tax",
        "compute_statutory_contribution": "compute_health_contribution",
        "retrieve_governed_knowledge": "lookup_grounded_knowledge",
        "explain_legal_basis": "lookup_grounded_knowledge",
        "compare_tax_periods_or_regimes": "lookup_grounded_knowledge",
        "produce_report": "generate_report_artifact",
        "produce_form": "generate_form_artifact",
        "extract_document": "extract_document",
        "retrieve_artifact_information": "retrieve_grounded_knowledge",
        "clarify_or_correct_request": "clarification_required",
        "unsupported_or_off_topic": "unknown",
    }
    return mapping.get(adjudication.primary_goal, "unknown")


def _derive_regime_identifier(taxpayer_facts: SemanticTaxpayerFacts) -> str | None:
    category = taxpayer_facts.taxpayer_category.value
    if isinstance(category, str) and category:
        if category in {"resident_employment", "resident_employment_plus_qualifying_interest"}:
            return "resident employment"
        if category == "non_resident_employment":
            return "non-resident employment"
        if category.startswith("sha_shif"):
            return "sha/shif"
        if category == "nhif_legacy":
            return "nhif legacy"
    employment_type = taxpayer_facts.employment_type.value
    if isinstance(employment_type, str) and employment_type:
        return employment_type
    return None


def _confidence_from_band(band: Literal["high", "medium", "low", "abstain"]) -> float:
    return {
        "high": 0.92,
        "medium": 0.68,
        "low": 0.35,
        "abstain": 0.0,
    }[band]


def _semantic_coerce_amount(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _semantic_coerce_frequency(value: object) -> Literal["monthly", "annual"] | None:
    if value in {"monthly", "annual"}:
        return cast(Literal["monthly", "annual"], value)
    return None


def _semantic_coerce_residency(value: object) -> Literal["resident", "non_resident"] | None:
    if value in {"resident", "non_resident"}:
        return cast(Literal["resident", "non_resident"], value)
    return None


def _semantic_coerce_filing_status(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None

    def _parse_extraction_response(self, response_text: str) -> ExtractedSemanticContext:
        """Parse structured extraction response from LLM."""

        try:
            # Remove markdown code blocks if present
            cleaned = response_text.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

            parsed = json.loads(cleaned)

            tax_year = parsed.get("tax_year")
            if tax_year is not None and not isinstance(tax_year, int):
                try:
                    tax_year = int(tax_year)
                except (ValueError, TypeError):
                    tax_year = None

            regime = parsed.get("regime")
            if regime is not None:
                regime = str(regime).strip() if regime else None

            intent_class = str(parsed.get("intent_class", "unknown")).strip()
            tax_domain_hint = str(parsed.get("tax_domain_hint", "unknown")).strip()
            confidence = float(parsed.get("confidence", 0.5))
            raw_inferred_fields = parsed.get("inferred_fields", [])
            raw_implicit_context = parsed.get("implicit_context", {})

            inferred_fields: list[str] = (
                [str(item) for item in cast(list[object], raw_inferred_fields)]
                if isinstance(raw_inferred_fields, list)
                else []
            )
            implicit_context: dict[str, object] = (
                {str(k): v for k, v in cast(dict[object, object], raw_implicit_context).items()}
                if isinstance(raw_implicit_context, dict)
                else {}
            )

            raw_is_tax_related = parsed.get("is_tax_related", True)
            is_tax_related = (
                bool(raw_is_tax_related) if isinstance(raw_is_tax_related, bool) else True
            )

            raw_requires_computation = parsed.get("requires_computation", False)
            requires_computation = (
                bool(raw_requires_computation)
                if isinstance(raw_requires_computation, bool)
                else False
            )
            stated_facts = _parse_stated_facts(parsed.get("stated_facts"))

            return ExtractedSemanticContext(
                tax_year=tax_year,
                regime=regime,
                intent_class=intent_class,
                tax_domain_hint=tax_domain_hint,
                confidence=confidence,
                inferred_fields=inferred_fields,
                implicit_context=implicit_context,
                extraction_status="extracted",
                is_tax_related=is_tax_related,
                requires_computation=requires_computation,
                stated_facts=stated_facts,
            )
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as error:
            raise SemanticExtractionError(
                error_code="semantic_extraction_parse_error",
                message=f"Failed to parse LLM extraction response: {str(error)}",
                reason_code="invalid_response_format",
                context={"response_preview": response_text[:200]},
            ) from error


_TAXPAYER_FACT_FIELDS = (
    "income_amount_kes",
    "income_frequency",
    "turnover_amount_kes",
    "residency_status",
    "filing_status",
)


def _parse_stated_facts(raw_facts: object) -> ExtractedTaxpayerFacts:
    """Normalize explicit, sufficiently confident taxpayer facts from model output."""

    source: dict[str, object] = (
        cast(dict[str, object], raw_facts) if isinstance(raw_facts, dict) else {}
    )
    raw_confidence = source.get("confidence_per_field")
    confidence_source: dict[str, object] = (
        cast(dict[str, object], raw_confidence) if isinstance(raw_confidence, dict) else {}
    )
    confidence_per_field = {
        field: _coerce_confidence(confidence_source.get(field)) for field in _TAXPAYER_FACT_FIELDS
    }
    facts: ExtractedTaxpayerFacts = {
        "income_amount_kes": None,
        "income_frequency": None,
        "turnover_amount_kes": None,
        "residency_status": None,
        "filing_status": None,
        "confidence_per_field": confidence_per_field,
    }
    if confidence_per_field["income_amount_kes"] >= FACT_EXTRACTION_MIN_CONFIDENCE:
        facts["income_amount_kes"] = _coerce_amount(source.get("income_amount_kes"))
    if confidence_per_field["turnover_amount_kes"] >= FACT_EXTRACTION_MIN_CONFIDENCE:
        facts["turnover_amount_kes"] = _coerce_amount(source.get("turnover_amount_kes"))
    if confidence_per_field["income_frequency"] >= FACT_EXTRACTION_MIN_CONFIDENCE:
        frequency = source.get("income_frequency")
        if frequency in {"monthly", "annual"}:
            facts["income_frequency"] = cast(Literal["monthly", "annual"], frequency)
    if confidence_per_field["residency_status"] >= FACT_EXTRACTION_MIN_CONFIDENCE:
        residency = source.get("residency_status")
        if residency in {"resident", "non_resident"}:
            facts["residency_status"] = cast(Literal["resident", "non_resident"], residency)
    if confidence_per_field["filing_status"] >= FACT_EXTRACTION_MIN_CONFIDENCE:
        filing_status = source.get("filing_status")
        if isinstance(filing_status, str) and filing_status.strip():
            facts["filing_status"] = filing_status.strip()
    return facts


def _coerce_confidence(value: object) -> float:
    """Return a bounded confidence value, treating invalid values as untrusted."""

    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0.0
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return 0.0
    return confidence


def _coerce_amount(value: object) -> float | None:
    """Return a finite numeric KES amount without interpreting free-form text."""

    if isinstance(value, bool):
        return None
    if not isinstance(value, int | float | str):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if math.isfinite(amount) else None


# ---------------------------------------------------------------------------
# Follow-up classification
# ---------------------------------------------------------------------------

_SUPPORTED_FOLLOWUP_TYPES = frozenset(
    {
        "recompute_tax_year",
        "reuse_same_case",
        "add_legal_basis",
        "generate_report",
        "generate_form",
        "artifact_reference_detail",
        "continue_knowledge_question",
    }
)


class FollowupClassification(TypedDict):
    """Represent the result of LLM-based follow-up detection."""

    is_followup: bool
    followup_type: str | None  # one of _SUPPORTED_FOLLOWUP_TYPES, or None
    confidence: float
    # The subject that an anaphora such as "it" resolves to.  This is
    # deliberately carried through the resolver rather than used only for
    # telemetry, because it is part of the retrieval safety boundary.
    resolved_referent: str | None


class FollowupClassifier:
    """Use OpenAI structured transition adjudication to classify follow-up continuity."""

    def __init__(self, *, config: FollowupClassificationConfig | None = None) -> None:
        self._config = config or load_followup_classification_config()
        self._transition_adjudicator = ConversationStateTransitionAdjudicator(
            config=self._config
        )

    @property
    def is_configured(self) -> bool:
        """Return whether structured transition adjudication is enabled."""
        return self._transition_adjudicator.is_configured

    def classify(
        self,
        *,
        prompt_text: str,
        prior_context_summary: str,
    ) -> FollowupClassification:
        """Classify whether prompt_text is a follow-up to the described prior turn."""

        current_semantic_frame = _build_synthetic_followup_semantic_frame(
            prompt_text=prompt_text,
            prior_context_summary=prior_context_summary,
        )
        proposal = self._transition_adjudicator.adjudicate_transition(
            prompt_text=prompt_text,
            current_semantic_frame=current_semantic_frame,
            recent_conversation_state=(),
            prior_context_summary=prior_context_summary,
        )
        return _transition_to_followup_classification(proposal)


def _transition_to_followup_classification(
    proposal: ConversationStateTransitionProposal,
) -> FollowupClassification:
    frame = proposal.semantic_frame_mapping()
    followup_type: str | None = None
    if proposal.primary_relationship.value in {
        "continuation",
        "correction",
        "refinement",
        "comparison",
        "replay",
        "artifact_transformation",
        "result_transformation",
        "clarification_answer",
    }:
        followup_mode = frame.get("followup_mode")
        followup_type = followup_mode if isinstance(followup_mode, str) else None
    if followup_type is None:
        if frame.get("intent_class") == "compute_plus_grounding":
            followup_type = "add_legal_basis"
        elif frame.get("intent_class") == "generate_report_artifact":
            followup_type = "generate_report"
        elif frame.get("intent_class") == "generate_form_artifact":
            followup_type = "generate_form"
    confidence = proposal.confidence_score if proposal.confidence_score is not None else 0.5
    return FollowupClassification(
        is_followup=proposal.primary_relationship.value
        not in {"standalone", "topic_shift", "unsupported"},
        followup_type=followup_type,
        confidence=confidence,
        resolved_referent=proposal.referenced_entity,
    )


def _build_synthetic_followup_semantic_frame(
    *,
    prompt_text: str,
    prior_context_summary: str,
) -> dict[str, object]:
    normalized_prompt = " ".join(prompt_text.strip().split()).lower()
    normalized_prior = " ".join(prior_context_summary.strip().split()).lower()
    has_meaningful_prior_context = bool(normalized_prior) and normalized_prior not in {
        "no prior conversation context available.",
        "prior conversation context is sparse.",
    }
    conversation_references = {
        "refers_to_prior_context": any(
            marker in normalized_prompt
            for marker in ("what about", "same", "too", "again", "and ", "it ", "its ", "that ")
        )
        or has_meaningful_prior_context,
        "topic_shift": any(
            marker in normalized_prompt
            for marker in ("football", "soccer", "recipe", "weather", "travel", "code")
        ),
    }
    intent_class = "lookup_grounded_knowledge"
    if any(marker in normalized_prompt for marker in ("legal basis", "legal authority", "statutory authority")):
        intent_class = "compute_plus_grounding"
    elif "report" in normalized_prompt:
        intent_class = "generate_report_artifact"
    elif "form" in normalized_prompt:
        intent_class = "generate_form_artifact"
    elif "extract document" in normalized_prompt or "document extraction" in normalized_prompt:
        intent_class = "extract_document"
    tax_domain_hint = _extract_followup_domain_from_summary(normalized_prior)
    if tax_domain_hint is None:
        tax_domain_hint = _extract_followup_domain_from_prompt(normalized_prompt)
    tax_year_match = re.search(r"\b(19\d{2}|20\d{2})\b", normalized_prompt)
    frame: dict[str, object] = {
        "normalized_prompt_text": prompt_text,
        "intent_class": intent_class,
        "tax_domain_hint": tax_domain_hint or "unknown",
        "conversation_references": conversation_references,
        "semantic_extraction_confidence": 0.9 if conversation_references["refers_to_prior_context"] else 0.5,
    }
    if tax_year_match is not None:
        frame["tax_year_hint"] = int(tax_year_match.group(1))
    return frame


def _extract_followup_domain_from_summary(prior_context_summary: str) -> str | None:
    if "domain: vat" in prior_context_summary:
        return "vat"
    if "domain: income_tax" in prior_context_summary:
        return "income_tax"
    if "domain: health_contribution" in prior_context_summary:
        return "health_contribution"
    if "domain: paye_generalized" in prior_context_summary:
        return "paye_generalized"
    return None


def _extract_followup_domain_from_prompt(normalized_prompt: str) -> str | None:
    if "vat" in normalized_prompt:
        return "vat"
    if "income tax" in normalized_prompt:
        return "income_tax"
    if "health contribution" in normalized_prompt or "shif" in normalized_prompt:
        return "health_contribution"
    if "paye" in normalized_prompt:
        return "paye_generalized"
    return None
