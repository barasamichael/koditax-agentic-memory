"""Normalize prompt input into a deterministic, schema-safe intent envelope."""

# ruff: noqa: E501

from __future__ import annotations

import re
from typing import cast
from typing import TypedDict
from typing import NotRequired
from typing import TYPE_CHECKING
import hashlib
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor

from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.request_timer import timed_print
from services.orchestration.app.trace_context import build_trace_id
from services.orchestration.app.prompt_semantic_extractor import UserContextSummary
from services.orchestration.app.prompt_semantic_extractor import ExtractedTaxpayerFacts
from services.orchestration.app.prompt_semantic_extractor import PromptSemanticExtractor
from services.orchestration.app.prompt_semantic_extractor import SemanticExtractionError
from services.orchestration.app.prompt_semantic_extractor import ExtractedSemanticContext
from services.orchestration.app.lexical_phrase_matching import find_phrase_match

SUPPORTED_LANE_HINTS: dict[tuple[str, str, int], str] = {
    (
        "resident employment",
        "KIT-VER-20210101-A",
        2021,
    ): "resident_employment_income_2021_01_01",
    (
        "non-resident employment",
        "KIT-VER-20210101-A",
        2021,
    ): "non_resident_employment_income_2021_01_01",
    (
        "resident employment",
        "KIT-VER-20230701-A",
        2023,
    ): "resident_employment_income_2023_07_01",
    (
        "non-resident employment",
        "KIT-VER-20230701-A",
        2023,
    ): "non_resident_employment_income_2023_07_01",
    (
        "resident employment plus qualifying interest",
        "KIT-VER-20230701-A",
        2023,
    ): "resident_employment_plus_qualifying_interest_2023_07_01",
}

_INCOME_TAX_VERSION_BY_YEAR = {
    2021: "KIT-VER-20210101-A",
    2023: "KIT-VER-20230701-A",
}

_HEALTH_CONTRIBUTION_VERSION_BY_YEAR = {
    2023: "HCH-VER-20221231-REG",
    2024: "HCH-VER-20241001-A",
    2025: "HCH-VER-20250228-PIT",
}

SUPPORTED_HEALTH_LANE_HINTS: dict[tuple[str, str, int], tuple[str, str]] = {
    (
        "nhif legacy",
        "HCH-VER-20100716-A",
        2012,
    ): (
        "health_contribution_nhif_legacy_v1_2010_07_16",
        "nhif_legacy",
    ),
    (
        "nhif legacy",
        "HCH-VER-20150401-A",
        2019,
    ): (
        "health_contribution_nhif_legacy_v1_2015_04_01",
        "nhif_legacy",
    ),
    (
        "nhif legacy",
        "HCH-VER-20210528-A",
        2022,
    ): (
        "health_contribution_nhif_legacy_v1_2021_05_28",
        "nhif_legacy",
    ),
    (
        "nhif legacy",
        "HCH-VER-20221231-REG",
        2023,
    ): (
        "health_contribution_nhif_legacy_v1_2022_12_31_reg",
        "nhif_legacy",
    ),
    (
        "nhif special member",
        "HCH-VER-20221231-REG",
        2023,
    ): (
        "health_contribution_nhif_legacy_v1_2022_12_31_reg",
        "nhif_legacy",
    ),
    (
        "sha/shif salaried",
        "HCH-VER-20241001-A",
        2024,
    ): (
        "health_contribution_sha_shif_v1_2024_10_01",
        "sha_shif",
    ),
    (
        "sha/shif non-salaried",
        "HCH-VER-20250228-PIT",
        2025,
    ): (
        "health_contribution_sha_shif_v1_2025_02_28_pit",
        "sha_shif",
    ),
    (
        "transition boundary nhif",
        "HCH-VER-20221231-REG",
        2023,
    ): (
        "health_contribution_nhif_legacy_v1_2022_12_31_reg",
        "transition_boundary",
    ),
    (
        "transition boundary sha",
        "HCH-VER-20241001-A",
        2024,
    ): (
        "health_contribution_sha_shif_v1_2024_10_01",
        "transition_boundary",
    ),
    (
        "transition boundary sha",
        "HCH-VER-20250228-PIT",
        2025,
    ): (
        "health_contribution_sha_shif_v1_2025_02_28_pit",
        "transition_boundary",
    ),
}

DOMAIN_PHRASES_BY_DOMAIN_HINT: dict[str, tuple[str, ...]] = {
    "vat": ("value added tax", "vat", "v a t"),
    "withholding_tax_generalized": ("withholding tax", "wht", "w h t"),
    "paye_generalized": ("pay as you earn", "pay-as-you-earn", "paye", "p a y e"),
    "health_contribution": (
        "health contribution",
        "health levy",
        "shif",
        "s h i f",
        "sha",
    ),
    "business_income_generalized": ("business income", "trading income"),
    "rental_income_generalized": ("rental income", "rent"),
}

SUPPORTED_GROUNDED_EXPLANATION_INTENT_CLASSES = frozenset(
    {"lookup_grounded_knowledge", "retrieve_grounded_knowledge"}
)

_TIMELINE_KNOWLEDGE_MARKERS = (
    "change over time",
    "over time",
    "timeline",
    "history of",
    "historical",
    "compare",
    "between ",
    "from ",
    "across ",
    "before and after",
)

_INCOME_TAX_VERSION_TOKEN = "kit-ver-"
_HEALTH_VERSION_TOKEN = "hch-ver-"

if TYPE_CHECKING:
    from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolution


class PromptIntentEnvelope(TypedDict):
    """Represent deterministic prompt-intent envelope fields."""

    normalized_prompt_text: str
    tax_domain_hint: str
    requested_lane_hint: str | None
    historical_version_hint: str | None
    tax_year_hint: int | None
    intent_class: str
    parsing_status: str
    prompt_class: str
    correlation_id: str
    trace_id: str
    regime_identifier_hint: NotRequired[str | None]
    clarification_reason_code: NotRequired[str | None]
    clarification_message: NotRequired[str | None]
    required_context_fields: NotRequired[tuple[str, ...]]
    provided_context_fields: NotRequired[tuple[str, ...]]
    missing_required_context_fields: NotRequired[tuple[str, ...]]
    candidate_service_families: NotRequired[tuple[str, ...]]
    planning_mode_hint: NotRequired[str]
    knowledge_route_mode_hint: NotRequired[str | None]
    semantic_extraction_status: NotRequired[str]
    semantic_extraction_confidence: NotRequired[float]
    semantic_inferred_fields: NotRequired[list[str]]
    contextualized_prompt: NotRequired[str]
    operation_mode: NotRequired[str]
    answerability: NotRequired[str]
    needs_knowledge_retrieval: NotRequired[bool]
    needs_computation: NotRequired[bool]
    needs_external_action: NotRequired[bool]
    needs_artifact_operation: NotRequired[bool]
    relationship: NotRequired[str]
    assumptions: NotRequired[list[str]]
    retrieval_tax_domain_filter: NotRequired[str | None]
    referenced_candidate_ids: NotRequired[tuple[str, ...]]
    stated_facts: NotRequired[ExtractedTaxpayerFacts]
    effective_prompt_text: NotRequired[str]


def build_prompt_intent_envelope_from_turn_resolution(
    *, turn_resolution: "ConversationTurnResolution", correlation_id: str, trace_id: str
) -> PromptIntentEnvelope:
    """Deterministically adapt canonical turn semantics for `/prompt/decide`.

    This intentionally performs no semantic extraction and must remain the only
    envelope construction used by the conversational decision endpoint.
    """
    timed_print("[ENVELOPE] About to project intent envelope from turn resolution")
    envelope: PromptIntentEnvelope = {
        "normalized_prompt_text": " ".join(turn_resolution.raw_prompt.strip().split()).lower(),
        "tax_domain_hint": turn_resolution.tax_domain_hint or "unknown",
        "requested_lane_hint": turn_resolution.supported_lane_id,
        "historical_version_hint": turn_resolution.historical_version_id,
        "tax_year_hint": turn_resolution.tax_year_hint,
        "intent_class": turn_resolution.intent_class,
        "parsing_status": "parsed_with_turn_resolution",
        "prompt_class": "orchestration_prompt_flow",
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "clarification_reason_code": turn_resolution.clarification_reason_code,
        "clarification_message": turn_resolution.clarification_question,
        "required_context_fields": tuple(turn_resolution.required_context_fields),
        "provided_context_fields": tuple(turn_resolution.provided_context_fields),
        "missing_required_context_fields": tuple(
            turn_resolution.missing_required_context_fields
        ),
        "candidate_service_families": tuple(turn_resolution.candidate_service_families),
        "planning_mode_hint": turn_resolution.operation_mode.value,
        "knowledge_route_mode_hint": "search" if turn_resolution.needs_knowledge_retrieval else None,
        "semantic_extraction_status": "conversation_turn_resolution",
        "semantic_extraction_confidence": turn_resolution.confidence,
        "semantic_inferred_fields": list(turn_resolution.retained_fields),
        "contextualized_prompt": " ".join(turn_resolution.contextualized_prompt.strip().split()),
        "operation_mode": turn_resolution.operation_mode.value,
        "answerability": turn_resolution.answerability.value,
        "needs_knowledge_retrieval": turn_resolution.needs_knowledge_retrieval,
        "needs_computation": turn_resolution.needs_computation,
        "needs_external_action": turn_resolution.needs_external_action,
        "needs_artifact_operation": turn_resolution.needs_artifact_operation,
        "relationship": turn_resolution.relationship.value,
        "regime_identifier_hint": turn_resolution.regime_identifier,
        "assumptions": list(turn_resolution.assumptions),
        "retrieval_tax_domain_filter": turn_resolution.retrieval_tax_domain_filter,
        "referenced_candidate_ids": tuple(turn_resolution.referenced_candidate_ids),
        "effective_prompt_text": " ".join(turn_resolution.contextualized_prompt.strip().split()),
    }
    timed_print(
        "[ENVELOPE] Projected intent envelope from turn resolution "
        f"intent_class={envelope['intent_class']!r} tax_domain_hint={envelope['tax_domain_hint']!r}"
    )
    return envelope


class RejectedPromptIntentContext(TypedDict):
    """Represent deterministic rejected context for malformed prompt input."""

    tax_domain_hint: str | None
    requested_lane_hint: str | None
    historical_version_hint: str | None
    tax_year_hint: int | None
    intent_class: str
    prompt_class: str


class KnowledgeRoutePayload(TypedDict):
    """Represent deterministic knowledge-route payload derived from a prompt."""

    route_mode: str
    query: NotRequired[str]
    source_type: NotRequired[str | None]
    tax_domain: str
    effective_date: NotRequired[str | None]
    start_date: NotRequired[str]
    end_date: NotRequired[str]
    tax_year: NotRequired[int]
    source_ids: NotRequired[tuple[str, ...]]
    anchor_ids: NotRequired[tuple[str, ...]]


class PromptIntentEnvelopeError(RuntimeError):
    """Represent deterministic intent-envelope parsing failure payload."""

    def __init__(
        self,
        *,
        error_code: str,
        reason: str,
        message: str,
        rejected_context: RejectedPromptIntentContext,
        correlation_id: str,
        trace_id: str,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.reason = reason
        self.message = message
        self.rejected_context = rejected_context
        self.correlation_id = correlation_id
        self.trace_id = trace_id

    def payload(self) -> dict[str, object]:
        """Return canonical deterministic malformed-input error payload."""

        return {
            "error_code": self.error_code,
            "message": self.message,
            "reason": self.reason,
            "rejected_context": self.rejected_context,
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
        }


def parse_income_tax_prompt_intent_envelope(
    prompt_text: str,
    user_context: UserContextSummary | None = None,
    conversation_history: list[str] | None = None,
    current_tax_year: int | None = None,
) -> PromptIntentEnvelope:
    """Parse one prompt into deterministic intent envelope for orchestration boundary."""

    normalized_prompt_text = _normalize_prompt_text(prompt_text)
    correlation_id = _sha256_hex(normalized_prompt_text)
    trace_id = build_trace_id(correlation_id)

    timed_print(
        f"[ENVELOPE] parse_income_tax_prompt_intent_envelope: normalized={normalized_prompt_text!r}"
    )

    if not normalized_prompt_text:
        timed_print("[ENVELOPE] BLOCK — empty_prompt_text: prompt is blank after normalization")
        emit_income_tax_audit_event(
            event_type="intent_parsed",
            status="rejected",
            correlation_id=correlation_id,
            trace_id=trace_id,
            context={"reason": "empty_prompt_text"},
        )
        raise PromptIntentEnvelopeError(
            error_code="invalid_prompt_input",
            reason="empty_prompt_text",
            message="Prompt text must be non-empty for intent envelope parsing.",
            rejected_context={
                "tax_domain_hint": None,
                "requested_lane_hint": None,
                "historical_version_hint": None,
                "tax_year_hint": None,
                "intent_class": "unknown",
                "prompt_class": "income_tax_prompt_flow",
            },
            correlation_id=correlation_id,
            trace_id=trace_id,
        )

    # Launch the LLM semantic extractor in a background thread so it runs concurrently
    # with the keyword rule pass below. The future is resolved after the rule pass
    # completes, and the ensemble decision is made before building the envelope.
    timed_print("[ENVELOPE] About to launch semantic extractor")
    llm_future: Future[ExtractedSemanticContext] | None = _launch_semantic_extractor(
        prompt_text=prompt_text,
        user_context=user_context,
        conversation_history=conversation_history,
        current_tax_year=current_tax_year,
    )
    timed_print("[ENVELOPE] Launched semantic extractor")

    timed_print("[ENVELOPE] About to collect semantic extraction result")
    llm_signal, llm_error = _collect_llm_future(llm_future)
    timed_print("[ENVELOPE] Collected semantic extraction result")

    if llm_error is not None:
        raise PromptIntentEnvelopeError(
            error_code=llm_error.error_code,
            reason=llm_error.reason_code,
            message=llm_error.message,
            rejected_context={
                "tax_domain_hint": "unknown",
                "requested_lane_hint": None,
                "historical_version_hint": None,
                "tax_year_hint": None,
                "intent_class": "unknown",
                "prompt_class": "income_tax_prompt_flow",
            },
            correlation_id=correlation_id,
            trace_id=trace_id,
        )

    if llm_signal is None:
        raise PromptIntentEnvelopeError(
            error_code="semantic_extraction_failed",
            reason="semantic_extraction_unavailable",
            message="Structured semantic extraction did not return a result.",
            rejected_context={
                "tax_domain_hint": "unknown",
                "requested_lane_hint": None,
                "historical_version_hint": None,
                "tax_year_hint": None,
                "intent_class": "unknown",
                "prompt_class": "income_tax_prompt_flow",
            },
            correlation_id=correlation_id,
            trace_id=trace_id,
        )

    timed_print("[ENVELOPE] About to build prompt intent envelope from semantic context")
    envelope = _build_prompt_intent_envelope_from_semantic_context(
        semantic_context=llm_signal,
        normalized_prompt_text=normalized_prompt_text,
        prompt_text=prompt_text,
        correlation_id=correlation_id,
        trace_id=trace_id,
    )
    timed_print(
        "[ENVELOPE] Built prompt intent envelope from semantic context "
        f"intent_class={envelope['intent_class']!r}"
    )
    timed_print("[ENVELOPE] About to validate prompt intent envelope")
    validate_income_tax_prompt_intent_envelope(envelope)
    timed_print("[ENVELOPE] Validated prompt intent envelope")
    emit_income_tax_audit_event(
        event_type="intent_parsed_with_semantic_extraction",
        status=envelope["parsing_status"],
        correlation_id=correlation_id,
        trace_id=trace_id,
        supported_lane_id=envelope.get("requested_lane_hint"),
        historical_version_id=envelope.get("historical_version_hint"),
        tax_year=envelope.get("tax_year_hint"),
        context={
            "extraction_confidence": envelope.get("semantic_extraction_confidence", 0.0),
            "inferred_fields": envelope.get("semantic_inferred_fields", []),
            "tax_year_hint": envelope.get("tax_year_hint"),
            "classification_source": envelope.get("semantic_extraction_status", "extracted"),
        },
    )
    timed_print(
        f"[ENVELOPE] resolved envelope: intent_class={envelope['intent_class']!r}  "
        f"tax_domain_hint={envelope['tax_domain_hint']!r}  parsing_status={envelope['parsing_status']!r}  "
        f"lane={envelope['requested_lane_hint']!r}  version={envelope['historical_version_hint']!r}  "
        f"year={envelope['tax_year_hint']!r}  "
        f"clarification_reason_code={envelope.get('clarification_reason_code')!r}"
    )
    emit_income_tax_audit_event(
        event_type="intent_parsed",
        status=envelope["parsing_status"],
        correlation_id=correlation_id,
        trace_id=trace_id,
        supported_lane_id=envelope.get("requested_lane_hint"),
        historical_version_id=envelope.get("historical_version_hint"),
        tax_year=envelope.get("tax_year_hint"),
        context={
            "intent_class": envelope["intent_class"],
            "tax_domain_hint": envelope["tax_domain_hint"],
            "prompt_class": "income_tax_prompt_flow",
            "clarification_reason_code": envelope.get("clarification_reason_code", "") or "",
            "planning_mode_hint": envelope.get("planning_mode_hint", "single_step"),
            "classification_source": envelope.get("semantic_extraction_status", "extracted"),
        },
    )
    return envelope

    # --- Keyword rule pass (unchanged logic) ---

    tax_domain_hint = _detect_tax_domain_hint(normalized_prompt_text)
    requested_lane_hint: str | None = None
    historical_version_hint: str | None = None
    tax_year_hint: int | None = None
    regime_identifier_hint: str | None = None
    clarification_reason_code: str | None = None
    clarification_message: str | None = None
    required_context_fields: tuple[str, ...] = ()
    candidate_service_families: tuple[str, ...] = ()
    planning_mode_hint = "single_step"
    knowledge_route_mode_hint: str | None = None
    parsing_status = "parsed_with_unsupported_scope_hint"
    intent_class = "unknown"
    regime_identifier_hint: str | None = None

    service_families = _detect_service_families(normalized_prompt_text)
    timed_print(f"[ENVELOPE] _detect_service_families: detected={service_families!r}")
    if _is_ambiguous_service_request(service_families):
        timed_print(
            f"[ENVELOPE] BLOCK — ambiguous_service_family: families={service_families!r} require clarification"
        )
        intent_class = "clarification_required"
        parsing_status = "clarification_required"
        clarification_reason_code = "ambiguous_service_family"
        clarification_message = (
            "Prompt can map to multiple governed service families and needs clarification."
        )
        candidate_service_families = tuple(sorted(service_families))
    else:
        knowledge_payload = extract_knowledge_route_payload(normalized_prompt_text)
        if knowledge_payload is not None and not _looks_like_compute_prompt(
            normalized_prompt_text, tax_domain_hint
        ):
            intent_class = (
                "retrieve_grounded_knowledge"
                if knowledge_payload["route_mode"] == "retrieve"
                else "lookup_grounded_knowledge"
            )
            tax_domain_hint = knowledge_payload["tax_domain"]
            parsing_status = "parsed"
            knowledge_route_mode_hint = knowledge_payload["route_mode"]
        elif _looks_like_mixed_compute_plus_grounding_prompt(
            normalized_prompt_text, tax_domain_hint
        ):
            parsed_compute = _parse_compute_context_for_mixed(
                normalized_prompt_text=normalized_prompt_text,
                tax_domain_hint=tax_domain_hint,
            )
            if parsed_compute is None:
                intent_class = "clarification_required"
                parsing_status = "clarification_required"
                clarification_reason_code = "missing_tax_year"
                clarification_message = (
                    "Mixed compute-and-grounding planning needs enough compute context to plan."
                )
                required_context_fields = ("tax_year", "historical_version_id")
            else:
                requested_lane_hint = parsed_compute["requested_lane_hint"]
                historical_version_hint = parsed_compute["historical_version_hint"]
                tax_year_hint = parsed_compute["tax_year_hint"]
                regime_identifier_hint = parsed_compute.get("regime_identifier_hint")
                intent_class = "compute_plus_grounding"
                parsing_status = "parsed"
                planning_mode_hint = "multi_step"
        elif _looks_like_compute_prompt(normalized_prompt_text, tax_domain_hint):
            tax_year_hint = _extract_tax_year_hint(normalized_prompt_text)
            historical_version_hint = _extract_historical_version_hint(
                normalized_prompt_text=normalized_prompt_text,
                tax_domain_hint=tax_domain_hint,
            )
            if tax_domain_hint == "income_tax":
                parsed_income_tax = _parse_income_tax_hints(normalized_prompt_text)
                if parsed_income_tax is not None:
                    requested_lane_hint = parsed_income_tax["requested_lane_hint"]
                    historical_version_hint = parsed_income_tax["historical_version_hint"]
                    tax_year_hint = parsed_income_tax["tax_year_hint"]
                    intent_class = "compute_income_tax"
                    parsing_status = "parsed"
                elif tax_year_hint is None or historical_version_hint is None:
                    intent_class = "clarification_required"
                    parsing_status = "clarification_required"
                    clarification_reason_code = _missing_compute_context_reason_code(
                        tax_year_hint=tax_year_hint,
                        historical_version_hint=historical_version_hint,
                    )
                    clarification_message = (
                        "Income-tax computation planning needs both tax year and governed "
                        "historical version context."
                    )
                    required_context_fields = _required_compute_context_fields(
                        tax_year_hint=tax_year_hint,
                        historical_version_hint=historical_version_hint,
                    )
                else:
                    intent_class = "compute_income_tax"
            elif tax_domain_hint == "health_contribution":
                parsed_health = _parse_health_contribution_hints(normalized_prompt_text)
                if parsed_health is not None:
                    requested_lane_hint = parsed_health["requested_lane_hint"]
                    historical_version_hint = parsed_health["historical_version_hint"]
                    tax_year_hint = parsed_health["tax_year_hint"]
                    regime_identifier_hint = parsed_health["regime_identifier_hint"]
                    intent_class = "compute_health_contribution"
                    parsing_status = "parsed"
                elif tax_year_hint is None or historical_version_hint is None:
                    intent_class = "clarification_required"
                    parsing_status = "clarification_required"
                    clarification_reason_code = _missing_compute_context_reason_code(
                        tax_year_hint=tax_year_hint,
                        historical_version_hint=historical_version_hint,
                    )
                    clarification_message = (
                        "Health-contribution planning needs both tax year and governed "
                        "historical version context."
                    )
                    required_context_fields = _required_compute_context_fields(
                        tax_year_hint=tax_year_hint,
                        historical_version_hint=historical_version_hint,
                    )
                else:
                    intent_class = "compute_health_contribution"
                    regime_identifier_hint = _fallback_health_regime_identifier(
                        _extract_health_lane_descriptor(normalized_prompt_text)
                    )
            else:
                intent_class = "unsupported_domain_request"
        elif "forms" in service_families:
            intent_class = _resolve_form_intent(tax_domain_hint)
            if intent_class is None:
                intent_class = "unsupported_domain_request"
            else:
                parsing_status = "parsed"
        elif "reports" in service_families:
            intent_class = _resolve_report_intent(tax_domain_hint)
            if intent_class is None:
                intent_class = "unsupported_domain_request"
            else:
                parsing_status = "parsed"
        elif "document_ai" in service_families:
            if tax_domain_hint == "income_tax":
                intent_class = "extract_document"
                parsing_status = "parsed"
            elif tax_domain_hint == "unknown":
                intent_class = "clarification_required"
                parsing_status = "clarification_required"
                clarification_reason_code = "ambiguous_service_family"
                clarification_message = (
                    "Document extraction planning needs explicit supported tax-domain context."
                )
                required_context_fields = ("tax_domain",)
            else:
                intent_class = "unsupported_domain_request"
        elif tax_domain_hint != "unknown":
            intent_class = "unsupported_domain_request"

    # --- LLM-first resolution ---
    # The LLM result is the primary classifier for intent, domain, computation need,
    # and tax year. The rule pass above only provides structured token extraction
    # (KIT-VER-* version strings, lane descriptors) that the LLM cannot produce as
    # exact registry-matched tokens. When the LLM is available its decisions win
    # unconditionally. The rule pass is a fallback for when LLM is unavailable.

    semantic_extraction_status = "not_attempted"
    semantic_extraction_confidence = 0.0
    semantic_inferred_fields: list[str] = []
    stated_facts: ExtractedTaxpayerFacts = {
        "income_amount_kes": None,
        "income_frequency": None,
        "turnover_amount_kes": None,
        "residency_status": None,
        "filing_status": None,
        "confidence_per_field": {},
    }

    llm_signal, llm_error = _collect_llm_future(llm_future)

    if llm_error is not None:
        raise PromptIntentEnvelopeError(
            error_code=llm_error.error_code,
            reason=llm_error.reason_code,
            message=llm_error.message,
            rejected_context={
                "tax_domain_hint": "unknown",
                "requested_lane_hint": None,
                "historical_version_hint": None,
                "tax_year_hint": None,
                "intent_class": "unknown",
                "prompt_class": "income_tax_prompt_flow",
            },
            correlation_id=correlation_id,
            trace_id=trace_id,
        )

    if llm_signal is not None:
        envelope = _build_prompt_intent_envelope_from_semantic_context(
            semantic_context=llm_signal,
            normalized_prompt_text=normalized_prompt_text,
            prompt_text=prompt_text,
            correlation_id=correlation_id,
            trace_id=trace_id,
        )
        validate_income_tax_prompt_intent_envelope(envelope)
        emit_income_tax_audit_event(
            event_type="intent_parsed_with_semantic_extraction",
            status=envelope["parsing_status"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            supported_lane_id=envelope.get("requested_lane_hint"),
            historical_version_id=envelope.get("historical_version_hint"),
            tax_year=envelope.get("tax_year_hint"),
            context={
                "extraction_confidence": envelope.get("semantic_extraction_confidence", 0.0),
                "inferred_fields": envelope.get("semantic_inferred_fields", []),
                "tax_year_hint": envelope.get("tax_year_hint"),
                "classification_source": envelope.get("semantic_extraction_status", "extracted"),
            },
        )
        timed_print(
            f"[ENVELOPE] resolved envelope: intent_class={envelope['intent_class']!r}  "
            f"tax_domain_hint={envelope['tax_domain_hint']!r}  parsing_status={envelope['parsing_status']!r}  "
            f"lane={envelope['requested_lane_hint']!r}  version={envelope['historical_version_hint']!r}  "
            f"year={envelope['tax_year_hint']!r}  "
            f"clarification_reason_code={envelope.get('clarification_reason_code')!r}"
        )
        emit_income_tax_audit_event(
            event_type="intent_parsed",
            status=envelope["parsing_status"],
            correlation_id=correlation_id,
            trace_id=trace_id,
            supported_lane_id=envelope.get("requested_lane_hint"),
            historical_version_id=envelope.get("historical_version_hint"),
            tax_year=envelope.get("tax_year_hint"),
            context={
                "intent_class": envelope["intent_class"],
                "tax_domain_hint": envelope["tax_domain_hint"],
                "prompt_class": "income_tax_prompt_flow",
                "clarification_reason_code": envelope.get("clarification_reason_code", "") or "",
                "planning_mode_hint": envelope.get("planning_mode_hint", "single_step"),
                "classification_source": envelope.get("semantic_extraction_status", "extracted"),
            },
        )
        return envelope

    if llm_error is not None:
        semantic_extraction_status = "failed"
        timed_print(
            f"[ENVELOPE] LLM extraction failed — falling back to rule pass  "
            f"error={llm_error.reason_code!r}  prompt={normalized_prompt_text!r}"
        )
        emit_income_tax_audit_event(
            event_type="semantic_extraction_failed",
            status="failed",
            correlation_id=correlation_id,
            trace_id=trace_id,
            context={
                "error_code": llm_error.error_code,
                "reason_code": llm_error.reason_code,
            },
        )

    elif llm_signal is not None and not llm_signal.get("is_tax_related", True):
        timed_print(
            f"[ENVELOPE] BLOCK — off_topic_prompt (LLM): LLM says not tax-related  "
            f"confidence={llm_signal.get('confidence')!r}  prompt={normalized_prompt_text!r}"
        )
        emit_income_tax_audit_event(
            event_type="intent_parsed",
            status="rejected",
            correlation_id=correlation_id,
            trace_id=trace_id,
            context={
                "reason": "off_topic_prompt",
                "llm_confidence": llm_signal["confidence"],
            },
        )
        raise PromptIntentEnvelopeError(
            error_code="off_topic_prompt",
            reason="off_topic_prompt",
            message="Prompt is not related to Kenyan tax topics and cannot be processed.",
            rejected_context={
                "tax_domain_hint": "unknown",
                "requested_lane_hint": None,
                "historical_version_hint": None,
                "tax_year_hint": None,
                "intent_class": "off_topic",
                "prompt_class": "income_tax_prompt_flow",
            },
            correlation_id=correlation_id,
            trace_id=trace_id,
        )

    elif llm_signal is not None:
        # LLM is available and the prompt is tax-related — apply LLM decisions
        # unconditionally for every dimension it owns.
        semantic_extraction_status = "extracted"
        semantic_extraction_confidence = llm_signal["confidence"]
        semantic_inferred_fields = list(llm_signal["inferred_fields"])
        stated_facts = llm_signal["stated_facts"]

        llm_intent = {
            "generate_form": "generate_form_artifact",
            "generate_report": "generate_report_artifact",
        }.get(llm_signal["intent_class"], llm_signal["intent_class"])
        llm_domain = llm_signal["tax_domain_hint"]
        llm_tax_year = llm_signal["tax_year"]
        llm_regime = llm_signal["regime"]
        llm_requires_computation: bool = llm_signal.get("requires_computation", True)

        timed_print(
            f"[ENVELOPE] LLM result: intent={llm_intent!r}  domain={llm_domain!r}  "
            f"year={llm_tax_year!r}  requires_computation={llm_requires_computation!r}  "
            f"confidence={llm_signal['confidence']!r}  rule_intent={intent_class!r}"
        )

        # intent_class — LLM wins, but honour requires_computation as the authority
        # on whether this is a compute or knowledge request. If LLM says no computation
        # is needed, route to knowledge regardless of what intent_class says.
        if not llm_requires_computation and llm_intent not in {
            "generate_form_artifact",
            "generate_report_artifact",
            "extract_document",
        }:
            if intent_class == "retrieve_grounded_knowledge":
                parsing_status = "parsed_with_semantic_extraction"
                timed_print(
                    "[ENVELOPE] preserving explicit direct-retrieval intent despite "
                    "generic LLM knowledge lookup classification"
                )
            else:
                intent_class = "lookup_grounded_knowledge"
                parsing_status = "parsed_with_semantic_extraction"
            clarification_reason_code = None
            clarification_message = None
            required_context_fields = ()
            if intent_class == "lookup_grounded_knowledge":
                requested_lane_hint = None
                historical_version_hint = None
                timed_print(
                    f"[ENVELOPE] requires_computation=False → intent forced to "
                    f"lookup_grounded_knowledge (LLM intent was {llm_intent!r})  "
                    f"prompt={normalized_prompt_text!r}"
                )
        elif llm_intent not in {"unknown", ""}:
            if llm_intent != intent_class:
                timed_print(f"[ENVELOPE] LLM overrides intent: {intent_class!r} → {llm_intent!r}")
            intent_class = llm_intent
            if parsing_status not in {"parsed", "parsed_with_semantic_extraction"}:
                parsing_status = "parsed_with_semantic_extraction"
            # If the LLM resolved a clarification-required intent, clear clarification fields.
            if clarification_reason_code is not None:
                clarification_reason_code = None
                clarification_message = None
                required_context_fields = ()
            semantic_inferred_fields = _append_if_absent(semantic_inferred_fields, "intent_class")

        # tax_domain_hint — LLM wins when it gives a non-unknown value.
        if llm_domain not in {"unknown", ""}:
            if llm_domain != tax_domain_hint:
                timed_print(
                    f"[ENVELOPE] LLM overrides domain: {tax_domain_hint!r} → {llm_domain!r}"
                )
            tax_domain_hint = llm_domain
            semantic_inferred_fields = _append_if_absent(semantic_inferred_fields, "tax_domain")

        # tax_year — LLM always provides a value (defaults to current year when not stated).
        if llm_tax_year is not None:
            if llm_tax_year != tax_year_hint:
                timed_print(
                    f"[ENVELOPE] LLM overrides tax_year: {tax_year_hint!r} → {llm_tax_year!r}"
                )
            tax_year_hint = llm_tax_year
            semantic_inferred_fields = _append_if_absent(semantic_inferred_fields, "tax_year")

        # regime — LLM fills in when rule pass missed it.
        if llm_regime is not None and regime_identifier_hint is None:
            regime_identifier_hint = llm_regime
            semantic_inferred_fields = _append_if_absent(semantic_inferred_fields, "regime")

        # After LLM has set intent and tax_year, attempt structured token extraction
        # for the fields only the rule pass can provide: historical_version_hint and
        # requested_lane_hint. These require exact KIT-VER-*/HCH-VER-* token matching
        # against a known registry — the LLM cannot reliably produce these.
        if intent_class in {
            "compute_income_tax",
            "compute_health_contribution",
            "compute_plus_grounding",
        }:
            if historical_version_hint is None:
                historical_version_hint = _extract_historical_version_hint(
                    normalized_prompt_text=normalized_prompt_text,
                    tax_domain_hint=tax_domain_hint,
                )
            if historical_version_hint is not None and requested_lane_hint is None:
                if tax_domain_hint == "income_tax":
                    parsed_income_tax = _parse_income_tax_hints(normalized_prompt_text)
                    if parsed_income_tax is not None:
                        requested_lane_hint = parsed_income_tax["requested_lane_hint"]
                        if tax_year_hint is None:
                            tax_year_hint = parsed_income_tax["tax_year_hint"]
                elif tax_domain_hint == "health_contribution":
                    parsed_health = _parse_health_contribution_hints(normalized_prompt_text)
                    if parsed_health is not None:
                        requested_lane_hint = parsed_health["requested_lane_hint"]
                        if regime_identifier_hint is None:
                            regime_identifier_hint = parsed_health["regime_identifier_hint"]
                        if tax_year_hint is None:
                            tax_year_hint = parsed_health["tax_year_hint"]

        emit_income_tax_audit_event(
            event_type="intent_parsed_with_semantic_extraction",
            status=parsing_status,
            correlation_id=correlation_id,
            trace_id=trace_id,
            context={
                "extraction_confidence": semantic_extraction_confidence,
                "inferred_fields": semantic_inferred_fields,
                "tax_year_hint": tax_year_hint,
                "requires_computation": llm_requires_computation,
            },
        )

    envelope: PromptIntentEnvelope = {
        "normalized_prompt_text": normalized_prompt_text,
        "tax_domain_hint": tax_domain_hint,
        "requested_lane_hint": requested_lane_hint,
        "historical_version_hint": historical_version_hint,
        "tax_year_hint": tax_year_hint,
        "intent_class": intent_class,
        "parsing_status": parsing_status,
        "prompt_class": "income_tax_prompt_flow",
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "regime_identifier_hint": regime_identifier_hint,
        "clarification_reason_code": clarification_reason_code,
        "clarification_message": clarification_message,
        "required_context_fields": required_context_fields,
        "candidate_service_families": candidate_service_families,
        "planning_mode_hint": planning_mode_hint,
        "knowledge_route_mode_hint": knowledge_route_mode_hint,
        "semantic_extraction_status": semantic_extraction_status,
        "semantic_extraction_confidence": semantic_extraction_confidence,
        "semantic_inferred_fields": semantic_inferred_fields,
        "stated_facts": stated_facts,
    }
    timed_print(
        f"[ENVELOPE] resolved envelope: intent_class={intent_class!r}  "
        f"tax_domain_hint={tax_domain_hint!r}  parsing_status={parsing_status!r}  "
        f"lane={requested_lane_hint!r}  version={historical_version_hint!r}  year={tax_year_hint!r}  "
        f"clarification_reason_code={clarification_reason_code!r}"
    )
    validate_income_tax_prompt_intent_envelope(envelope)
    emit_income_tax_audit_event(
        event_type="intent_parsed",
        status=parsing_status,
        correlation_id=correlation_id,
        trace_id=trace_id,
        supported_lane_id=requested_lane_hint,
        historical_version_id=historical_version_hint,
        tax_year=tax_year_hint,
        context={
            "intent_class": intent_class,
            "tax_domain_hint": tax_domain_hint,
            "prompt_class": "income_tax_prompt_flow",
            "clarification_reason_code": clarification_reason_code or "",
            "planning_mode_hint": planning_mode_hint,
            "classification_source": semantic_extraction_status,
        },
    )
    return envelope


def validate_income_tax_prompt_intent_envelope(
    envelope: PromptIntentEnvelope,
) -> None:
    """Validate one prompt intent envelope with deterministic runtime checks."""

    required_string_fields = (
        "normalized_prompt_text",
        "tax_domain_hint",
        "intent_class",
        "parsing_status",
        "prompt_class",
        "correlation_id",
        "trace_id",
    )
    for field_name in required_string_fields:
        value = envelope.get(field_name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Prompt intent envelope field '{field_name}' is invalid.")


def _build_prompt_intent_envelope_from_semantic_context(
    *,
    semantic_context: ExtractedSemanticContext,
    normalized_prompt_text: str,
    prompt_text: str,
    correlation_id: str,
    trace_id: str,
) -> PromptIntentEnvelope:
    timed_print("[ENVELOPE] About to project prompt envelope from semantic context")
    semantic_frame = semantic_context.get("semantic_frame", {})
    if not semantic_frame:
        semantic_frame = _build_semantic_frame_from_legacy_context(
            semantic_context=semantic_context,
            normalized_prompt_text=normalized_prompt_text,
        )

    primary_goal = _semantic_string(
        semantic_frame.get("primary_goal"),
        default=str(semantic_context.get("intent_class", "unknown")),
    )
    primary_tax_domain = _semantic_string(
        semantic_frame.get("primary_tax_domain"),
        default=str(semantic_context.get("tax_domain_hint", "unknown")),
    )
    temporal_scope = cast(dict[str, object], semantic_frame.get("temporal_scope", {}))
    taxpayer_facts = cast(dict[str, object], semantic_frame.get("taxpayer_facts", {}))
    clarification_required = bool(
        semantic_frame.get("clarification_required")
        or semantic_frame.get("adjudication_status") == "clarification_required"
    )
    abstained = bool(semantic_frame.get("abstained"))
    confidence_score: float | None = cast(float | None, semantic_context.get("confidence"))
    if confidence_score is None:
        confidence_score = cast(float | None, semantic_frame.get("confidence_score"))

    tax_year_hint = _semantic_int(temporal_scope.get("explicit_tax_year"))
    if tax_year_hint is None:
        tax_year_hint = semantic_context.get("tax_year")
    historical_version_hint = _semantic_string(
        temporal_scope.get("explicit_version_identifier"),
        default=cast(str, semantic_context.get("historical_version_hint", "")) or "",
    ) or None
    if historical_version_hint is None and tax_year_hint is not None:
        if primary_tax_domain == "income_tax":
            historical_version_hint = _INCOME_TAX_VERSION_BY_YEAR.get(tax_year_hint)
        elif primary_tax_domain == "health_contribution":
            historical_version_hint = _HEALTH_CONTRIBUTION_VERSION_BY_YEAR.get(tax_year_hint)
    requested_lane_hint = _resolve_lane_hint_from_semantic_frame(
        domain=primary_tax_domain,
        taxpayer_facts=taxpayer_facts,
        historical_version_hint=historical_version_hint,
        tax_year_hint=tax_year_hint,
        normalized_prompt_text=normalized_prompt_text,
    )

    requested_capabilities: list[str] = []
    for item in cast(list[object], semantic_frame.get("requested_capabilities", [])):
        if isinstance(item, str):
            capability = item.strip()
            if capability:
                requested_capabilities.append(capability)
    requested_deliverables: list[str] = []
    for item in cast(list[object], semantic_frame.get("requested_deliverables", [])):
        if isinstance(item, str):
            deliverable = item.strip()
            if deliverable:
                requested_deliverables.append(deliverable)

    if primary_goal == "unsupported_or_off_topic" or (abstained and primary_tax_domain == "unknown"):
        raise PromptIntentEnvelopeError(
            error_code="off_topic_prompt",
            reason="off_topic_prompt",
            message="Prompt is not related to supported Kenyan tax topics and cannot be processed.",
            rejected_context={
                "tax_domain_hint": "unknown",
                "requested_lane_hint": None,
                "historical_version_hint": None,
                "tax_year_hint": None,
                "intent_class": "off_topic",
                "prompt_class": "income_tax_prompt_flow",
            },
            correlation_id=correlation_id,
            trace_id=trace_id,
        )

    compute_requested = _has_explicit_computation_request(normalized_prompt_text)
    normalized_prompt_lower = normalized_prompt_text.lower()
    intent_class = _map_primary_goal_to_intent_class(
        primary_goal=primary_goal,
        requested_capabilities=requested_capabilities,
        requested_deliverables=requested_deliverables,
    )
    parsing_status = "parsed"
    if primary_tax_domain == "unknown":
        parsing_status = "parsed_with_unsupported_scope_hint"
    if clarification_required and primary_tax_domain != "unknown":
        intent_class = "unknown"
        parsing_status = "parsed_with_unsupported_scope_hint"
    if compute_requested and primary_tax_domain not in {"income_tax", "health_contribution"}:
        intent_class = "unsupported_domain_request"
        parsing_status = "parsed_with_unsupported_scope_hint"
    elif clarification_required and primary_tax_domain == "unknown":
        intent_class = "unsupported_domain_request"

    regime_identifier_hint: str | None = None
    if intent_class in {"unknown", "clarification_required", "unsupported_domain_request"}:
        if "generate report" in normalized_prompt_lower or "report" in normalized_prompt_lower:
            intent_class = "generate_report_artifact"
            parsing_status = "parsed"
            clarification_reason_code = None
            clarification_message = None
            required_context_fields = ()
        elif "generate form" in normalized_prompt_lower or "form" in normalized_prompt_lower:
            intent_class = "generate_form_artifact"
            parsing_status = "parsed"
            clarification_reason_code = None
            clarification_message = None
            required_context_fields = ()
    if primary_tax_domain == "health_contribution" and regime_identifier_hint is None:
        if "sha/shif salaried" in normalized_prompt_lower or "sha shif salaried" in normalized_prompt_lower:
            regime_identifier_hint = "sha_shif"
        elif "sha/shif non-salaried" in normalized_prompt_lower or "sha shif non-salaried" in normalized_prompt_lower:
            regime_identifier_hint = "sha_shif"
        elif "nhif" in normalized_prompt_lower:
            regime_identifier_hint = "nhif_legacy"
    if primary_tax_domain == "health_contribution" and regime_identifier_hint is None:
        if "sha/shif salaried" in normalized_prompt_lower or "sha shif salaried" in normalized_prompt_lower:
            regime_identifier_hint = "sha_shif"
        elif "sha/shif non-salaried" in normalized_prompt_lower or "sha shif non-salaried" in normalized_prompt_lower:
            regime_identifier_hint = "sha_shif"
        elif "nhif" in normalized_prompt_lower:
            regime_identifier_hint = "nhif_legacy"

    candidate_service_families = _candidate_service_families_from_capabilities(
        requested_capabilities=requested_capabilities,
        requested_deliverables=requested_deliverables,
    )
    planning_mode_hint = (
        "multi_step"
        if len(requested_capabilities) > 1
        or "legal_basis_explanation" in requested_capabilities
        or intent_class == "compute_plus_grounding"
        else "single_step"
    )
    knowledge_route_mode_hint = _knowledge_route_mode_hint_from_semantic_frame(
        requested_capabilities=requested_capabilities,
        temporal_scope=temporal_scope,
    )

    clarification_reason_code = None
    clarification_message = None
    required_context_fields: tuple[str, ...] = ()
    if clarification_required:
        clarification_proposal = cast(dict[str, object] | None, semantic_frame.get("clarification_proposal"))
        clarification_reason_code = _semantic_string(
            clarification_proposal.get("reason") if clarification_proposal is not None else None,
            default="clarification_required",
        )
        clarification_message = _semantic_string(
            clarification_proposal.get("question") if clarification_proposal is not None else None,
            default="Prompt requires clarification before deterministic planning can continue.",
        )
        required_context_fields = tuple(
            str(item)
            for item in cast(
                list[object],
                semantic_frame.get("missing_required_facts", []),
            )
            if isinstance(item, str)
        )
    elif primary_tax_domain == "unknown" and intent_class in {"unknown", "unsupported_domain_request"}:
        clarification_reason_code = "ambiguous_tax_domain"
        clarification_message = "Prompt requires supported tax-domain clarification."
        required_context_fields = ("tax_domain",)

    envelope: PromptIntentEnvelope = {
        "normalized_prompt_text": normalized_prompt_text,
        "tax_domain_hint": primary_tax_domain,
        "requested_lane_hint": requested_lane_hint,
        "historical_version_hint": historical_version_hint,
        "tax_year_hint": tax_year_hint,
        "intent_class": intent_class,
        "parsing_status": parsing_status,
        "prompt_class": "income_tax_prompt_flow",
        "correlation_id": correlation_id,
        "trace_id": trace_id,
    }
    if clarification_reason_code is not None:
        envelope["clarification_reason_code"] = clarification_reason_code
    if clarification_message is not None:
        envelope["clarification_message"] = clarification_message
    if required_context_fields:
        envelope["required_context_fields"] = required_context_fields
    if clarification_required and len(candidate_service_families) > 1:
        envelope["candidate_service_families"] = tuple(candidate_service_families)
    if planning_mode_hint != "single_step":
        envelope["planning_mode_hint"] = planning_mode_hint
    if knowledge_route_mode_hint is not None:
        envelope["knowledge_route_mode_hint"] = knowledge_route_mode_hint
    if (
        envelope["intent_class"] == "unknown"
        and envelope["tax_domain_hint"] != "unknown"
        and _looks_like_explicit_knowledge_question(normalized_prompt_text)
    ):
        envelope["intent_class"] = "lookup_grounded_knowledge"
        envelope["parsing_status"] = "parsed"
    timed_print(
        "[ENVELOPE] Projected prompt envelope from semantic context "
        f"intent_class={envelope['intent_class']!r} tax_domain_hint={envelope['tax_domain_hint']!r}"
    )
    return envelope


def _has_explicit_computation_request(normalized_prompt_text: str) -> bool:
    """Return whether the prompt explicitly asks for a numerical calculation."""

    return any(
        marker in normalized_prompt_text
        for marker in (
            "compute ",
            "calculate ",
            "determine ",
            "how much ",
            "work out ",
            "what is my tax liability",
            "calculate tax",
            "how much tax",
            "compute paye",
        )
    )


def _looks_like_explicit_knowledge_question(normalized_prompt_text: str) -> bool:
    """Return whether the prompt is a standalone knowledge question."""

    normalized = normalized_prompt_text.strip().lower()
    return any(
        normalized.startswith(prefix)
        for prefix in (
            "what is ",
            "who is ",
            "when is ",
            "where is ",
            "which ",
            "how is ",
            "why is ",
            "explain ",
        )
    )


def extract_knowledge_route_payload(
    prompt_text: str,
) -> KnowledgeRoutePayload | None:
    """Return deterministic knowledge-route payload for supported lookup prompts."""

    collapsed_prompt_text = _normalize_prompt_text(prompt_text)
    direct_retrieval = _parse_knowledge_direct_retrieval(collapsed_prompt_text)
    if direct_retrieval is not None:
        return direct_retrieval

    authority_lookup = _parse_knowledge_authority_lookup(collapsed_prompt_text)
    if authority_lookup is not None:
        return authority_lookup

    timeline_lookup = _parse_knowledge_timeline_lookup(collapsed_prompt_text)
    if timeline_lookup is not None:
        return timeline_lookup

    # Fallback: treat any on-topic prompt as a free-text search so that plain
    # questions like "What is income tax?" reach the knowledge base instead of
    # being rejected for lacking an explicit authority-lookup marker.
    # _extract_knowledge_tax_domain only matches narrow positional patterns, so
    # use the broader _detect_tax_domain_hint which scans for keywords anywhere.
    _raw_domain = _detect_tax_domain_hint(collapsed_prompt_text)
    tax_domain = _raw_domain if _raw_domain != "unknown" else None
    if tax_domain is None:
        return None
    query = prompt_text.strip()
    return {
        "route_mode": "search",
        "query": query,
        "tax_domain": tax_domain,
    }


def supports_grounded_explanation_intent(intent_class: str) -> bool:
    """Return whether one parsed prompt intent supports grounded explanation rendering."""

    return intent_class in SUPPORTED_GROUNDED_EXPLANATION_INTENT_CLASSES


def _normalize_prompt_text(prompt_text: str) -> str:
    return " ".join(prompt_text.strip().split()).lower()


def _detect_tax_domain_hint(normalized_prompt_text: str) -> str:
    """Classify the tax domain of a prompt using keyword matching.

    This is the fast rule pass — it identifies which tax domain the prompt
    is about. It does NOT decide whether computation is required; that
    decision belongs to the LLM signal (requires_computation field) resolved
    in the ensemble step after this pass.
    """
    for domain_hint, phrases in DOMAIN_PHRASES_BY_DOMAIN_HINT.items():
        match = find_phrase_match(normalized_prompt_text, phrases)
        if match is not None:
            timed_print(
                "[ENVELOPE] _detect_tax_domain_hint: "
                f"matched_phrase={match['matched_phrase']!r} "
                f"normalized_span={match['normalized_span']!r} "
                f"start_index={match['start_index']} end_index={match['end_index']} "
                f"boundary_validation='token_boundary_safe' "
                f"selected_domain={domain_hint!r}"
            )
            return domain_hint
    if find_phrase_match(normalized_prompt_text, ("income tax",)) is not None:
        timed_print(
            f"[ENVELOPE] _detect_tax_domain_hint: fallback matched_phrase='income tax' "
            f"selected_domain='income_tax' prompt={normalized_prompt_text!r}"
        )
        return "income_tax"
    timed_print(
        f"[ENVELOPE] _detect_tax_domain_hint: no keyword matched → domain_hint='unknown'  prompt={normalized_prompt_text!r}"
    )
    return "unknown"


def _parse_knowledge_authority_lookup(
    normalized_prompt_text: str,
) -> KnowledgeRoutePayload | None:
    authority_scope: str | None = None
    effective_date: str | None = None
    query_text = normalized_prompt_text
    if "effective " in query_text:
        before_effective, _, after_effective = query_text.partition("effective ")
        query_text = before_effective.strip()
        effective_candidate = after_effective.split(" ", 1)[0].rstrip(".")
        if _looks_like_iso_date(effective_candidate):
            effective_date = effective_candidate
    for scope in ("statutory", "regulation", "guidance", "commentary", "legal"):
        marker = f"{scope} authority for "
        if marker in query_text:
            authority_scope = scope
            _, _, query_text = query_text.partition(marker)
            break
    if authority_scope is None:
        for marker in (
            "authority for ",
            "legal basis for ",
            "legal authority for ",
            "what law governs ",
        ):
            if marker in query_text:
                authority_scope = "legal"
                _, _, query_text = query_text.partition(marker)
                break
    if authority_scope is None:
        return None
    tax_domain = _extract_knowledge_tax_domain(query_text)
    if tax_domain is None:
        return None
    query = query_text
    for suffix in (
        f" in {tax_domain.replace('_', ' ')}",
        f" under {tax_domain.replace('_', ' ')}",
    ):
        if suffix in query:
            query = query.rsplit(suffix, 1)[0]
            break
    query = query.strip(" .")
    if not query:
        return None
    timeline_window = _resolve_knowledge_timeline_window(normalized_prompt_text)
    if timeline_window is not None:
        timeline_payload = cast(
            KnowledgeRoutePayload,
            {
                "route_mode": "timeline_search",
                "query": query,
                "source_type": _knowledge_source_type_for_scope(authority_scope),
                "tax_domain": tax_domain,
                "start_date": timeline_window["start_date"],
                "end_date": timeline_window["end_date"],
                "tax_year": timeline_window["tax_year"],
            },
        )
        return timeline_payload
    search_payload = cast(
        KnowledgeRoutePayload,
        {
            "route_mode": "search",
            "query": query,
            "source_type": _knowledge_source_type_for_scope(authority_scope),
            "tax_domain": tax_domain,
            "effective_date": effective_date,
        },
    )
    return search_payload


def _parse_knowledge_timeline_lookup(
    normalized_prompt_text: str,
) -> KnowledgeRoutePayload | None:
    tax_domain = _detect_tax_domain_hint(normalized_prompt_text)
    if tax_domain == "unknown":
        return None
    timeline_window = _resolve_knowledge_timeline_window(normalized_prompt_text)
    if timeline_window is None:
        return None
    timeline_payload = cast(
        KnowledgeRoutePayload,
        {
            "route_mode": "timeline_search",
            "query": normalized_prompt_text.strip(),
            "tax_domain": tax_domain,
            "start_date": timeline_window["start_date"],
            "end_date": timeline_window["end_date"],
            "tax_year": timeline_window["tax_year"],
        },
    )
    return timeline_payload


def _parse_knowledge_direct_retrieval(
    normalized_prompt_text: str,
) -> KnowledgeRoutePayload | None:
    prefix_options = (
        "retrieve grounded knowledge for ",
        "get grounded knowledge for ",
    )
    prefix = next(
        (item for item in prefix_options if normalized_prompt_text.startswith(item)),
        None,
    )
    if prefix is None:
        return None
    remainder = normalized_prompt_text.removeprefix(prefix).rstrip(".")
    tax_domain = _extract_knowledge_tax_domain(remainder)
    if tax_domain is None:
        return None
    if " source " not in remainder or " anchor " not in remainder:
        return None
    _, _, source_and_anchor = remainder.partition(" source ")
    source_id, _, anchor_section = source_and_anchor.partition(" anchor ")
    anchor_id = anchor_section.strip()
    source_id = source_id.strip()
    if not source_id or not anchor_id:
        return None
    return {
        "route_mode": "retrieve",
        "tax_domain": tax_domain,
        "source_ids": (source_id,),
        "anchor_ids": (anchor_id,),
    }


def _extract_knowledge_tax_domain(query_text: str) -> str | None:
    for phrase, normalized in (
        ("income tax", "income_tax"),
        ("health contribution", "health_contribution"),
        ("paye", "paye_generalized"),
    ):
        if f" in {phrase}" in query_text or f" under {phrase}" in query_text:
            return normalized
        if query_text.startswith(f"{phrase} "):
            return normalized
    return None


def _resolve_knowledge_timeline_window(
    normalized_prompt_text: str,
) -> dict[str, str | int] | None:
    years = sorted(
        {int(value) for value in re.findall(r"\b(19\d{2}|20\d{2})\b", normalized_prompt_text)}
    )
    has_timeline_marker = any(
        marker in normalized_prompt_text for marker in _TIMELINE_KNOWLEDGE_MARKERS
    )
    if len(years) >= 2:
        start_year = years[0]
        end_year = years[-1]
        return {
            "start_date": f"{start_year:04d}-01-01",
            "end_date": f"{end_year:04d}-12-31",
            "tax_year": start_year if start_year == end_year else end_year,
        }
    if len(years) == 1 and (
        has_timeline_marker
        or any(
            token in normalized_prompt_text
            for token in (
                f"in {years[0]}",
                f"for {years[0]}",
                f"during {years[0]}",
                f"as of {years[0]}",
            )
        )
    ):
        year = years[0]
        return {
            "start_date": f"{year:04d}-01-01",
            "end_date": f"{year:04d}-12-31",
            "tax_year": year,
        }
    if has_timeline_marker and years:
        year = years[0]
        return {
            "start_date": f"{year:04d}-01-01",
            "end_date": f"{year:04d}-12-31",
            "tax_year": year,
        }
    return None


def _knowledge_source_type_for_scope(authority_scope: str) -> str:
    normalized = authority_scope.strip().lower()
    if normalized in {"statutory", "legal"}:
        return "tax_law"
    return normalized


def _missing_compute_context_reason_code(
    *,
    tax_year_hint: int | None,
    historical_version_hint: str | None,
) -> str:
    if tax_year_hint is None:
        return "missing_tax_year"
    if historical_version_hint is None:
        return "missing_regime_version_context"
    return "missing_compute_context"


def _required_compute_context_fields(
    *,
    tax_year_hint: int | None,
    historical_version_hint: str | None,
) -> tuple[str, ...]:
    missing: list[str] = []
    if tax_year_hint is None:
        missing.append("tax_year")
    if historical_version_hint is None:
        missing.append("historical_version_id")
    return tuple(missing)


def _looks_like_iso_date(value: str) -> bool:
    parts = value.split("-")
    return len(parts) == 3 and all(part.isdigit() for part in parts)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Ensemble classification helpers
# ---------------------------------------------------------------------------


def _launch_semantic_extractor(
    *,
    prompt_text: str,
    user_context: UserContextSummary | None,
    conversation_history: list[str] | None,
    current_tax_year: int | None,
) -> Future[ExtractedSemanticContext] | None:
    """Submit a structured semantic adjudication to a background thread."""
    timed_print("[ENVELOPE] About to submit semantic extractor future")
    extractor = PromptSemanticExtractor()
    executor = ThreadPoolExecutor(max_workers=1)
    future: Future[ExtractedSemanticContext] = executor.submit(
        extractor.extract,
        prompt_text,
        user_context,
        conversation_history,
        current_tax_year,
    )
    executor.shutdown(wait=False)
    timed_print("[ENVELOPE] Submitted semantic extractor future")
    return future


def _collect_llm_future(
    future: Future[ExtractedSemanticContext] | None,
) -> tuple[ExtractedSemanticContext | None, SemanticExtractionError | None]:
    """Block for the LLM future and return (result, error). Both are None when no future."""
    if future is None:
        return None, None
    try:
        timed_print("[ENVELOPE] About to collect semantic extractor future result")
        result = future.result()
        timed_print("[ENVELOPE] Collected semantic extractor future result")
        return result, None
    except SemanticExtractionError as exc:
        return None, exc
    except Exception as exc:
        # Unexpected executor errors are surfaced as a generic extraction failure.
        return None, SemanticExtractionError(
            error_code="semantic_extraction_failed",
            message=f"Unexpected extraction error: {exc}",
            reason_code="extraction_unexpected_error",
        )


def _append_if_absent(fields: list[str], field: str) -> list[str]:
    """Return a new list with field appended only when not already present."""
    if field in fields:
        return fields
    return [*fields, field]


def _semantic_string(value: object, default: str | None = "") -> str:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    if default is None:
        return ""
    return default


def _semantic_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _build_semantic_frame_from_legacy_context(
    *,
    semantic_context: ExtractedSemanticContext,
    normalized_prompt_text: str,
) -> dict[str, object]:
    stated_facts = cast(dict[str, object], semantic_context.get("stated_facts", {}))
    tax_domain_hint = str(semantic_context.get("tax_domain_hint", "unknown"))
    intent_class = str(semantic_context.get("intent_class", "unknown"))
    requested_capabilities: list[str] = []
    requested_deliverables: list[str] = []
    if intent_class in {"compute_income_tax", "compute_health_contribution"}:
        requested_capabilities.append("computation")
        requested_deliverables.append("calculation")
        if any(
            marker in normalized_prompt_text
            for marker in (
                "legal basis",
                "legal authority",
                "statutory authority",
                "cite the law",
                "explain the legal basis",
            )
        ):
            requested_capabilities.append("legal_basis_explanation")
            requested_deliverables.extend(["explanation", "legal_authority"])
    elif intent_class in {"lookup_grounded_knowledge", "retrieve_grounded_knowledge"}:
        requested_capabilities.append("governed_knowledge_retrieval")
        requested_deliverables.append("direct_answer")
    elif intent_class == "generate_report_artifact":
        requested_capabilities.append("report_generation")
        requested_deliverables.append("report")
    elif intent_class == "generate_form_artifact":
        requested_capabilities.append("form_generation")
        requested_deliverables.append("form")
    elif intent_class == "extract_document":
        requested_capabilities.append("document_extraction")
        requested_deliverables.append("document_analysis")

    taxpayer_category = stated_facts.get("taxpayer_category")
    if isinstance(taxpayer_category, str) and taxpayer_category:
        category_value = taxpayer_category
    else:
        category_value = None
    temporal_scope: dict[str, object] = {
        "explicit_tax_year": semantic_context.get("tax_year"),
        "explicit_version_identifier": semantic_context.get("historical_version_hint"),
        "historical_request": bool(semantic_context.get("historical_version_hint")),
        "current_period_requested": False,
        "comparison_years": [],
        "unresolved_temporal_scope": semantic_context.get("tax_year") is None
        and semantic_context.get("historical_version_hint") is None,
    }
    return {
        "schema_version": "2026-07-26",
        "adjudication_status": "clarification_required"
        if semantic_context.get("clarification_reason_code")
        else "adjudicated",
        "confidence_band": "medium",
        "confidence_score": semantic_context.get("semantic_extraction_confidence", 0.5),
        "abstained": semantic_context.get("intent_class") == "unknown",
        "clarification_required": semantic_context.get("clarification_reason_code") is not None,
        "semantic_rationale": "legacy semantic context fallback",
        "language_mode": "en",
        "primary_goal": _legacy_primary_goal_from_intent_class(intent_class),
        "secondary_goals": [],
        "primary_tax_domain": tax_domain_hint,
        "secondary_tax_domains": [],
        "requested_capabilities": requested_capabilities,
        "action_class": "uncertain_action_class",
        "requested_deliverables": requested_deliverables,
        "temporal_scope": temporal_scope,
        "taxpayer_facts": {
            "income_amount_kes": {
                "status": "absent",
                "value": None,
                "confidence": 0.0,
            },
            "income_frequency": {
                "status": "absent",
                "value": None,
                "confidence": 0.0,
            },
            "turnover_amount_kes": {
                "status": "absent",
                "value": None,
                "confidence": 0.0,
            },
            "residency_status": {
                "status": "absent",
                "value": None,
                "confidence": 0.0,
            },
            "filing_status": {
                "status": "absent",
                "value": None,
                "confidence": 0.0,
            },
            "taxpayer_category": {
                "status": "explicit" if category_value else "absent",
                "value": category_value,
                "confidence": 0.8 if category_value else 0.0,
            },
            "employment_type": {
                "status": "absent",
                "value": None,
                "confidence": 0.0,
            },
            "qualifying_interest": {
                "status": "absent",
                "value": None,
                "confidence": 0.0,
            },
        },
        "conversation_references": {
            "refers_to_prior_context": False,
            "referenced_execution_or_artifact": None,
            "reused_facts": [],
            "replaced_facts": [],
            "corrections": [],
            "topic_shift": False,
            "unresolved_references": [],
        },
        "ambiguous_fields": [],
        "missing_required_facts": [],
        "conflicting_user_statements": [],
        "unsupported_concepts": [],
        "clarification_proposal": None,
    }


def _legacy_primary_goal_from_intent_class(intent_class: str) -> str:
    return {
        "compute_income_tax": "compute_tax_obligation",
        "compute_health_contribution": "compute_statutory_contribution",
        "lookup_grounded_knowledge": "retrieve_governed_knowledge",
        "retrieve_grounded_knowledge": "retrieve_artifact_information",
        "generate_report_artifact": "produce_report",
        "generate_form_artifact": "produce_form",
        "extract_document": "extract_document",
        "clarification_required": "clarify_or_correct_request",
    }.get(intent_class, "unsupported_or_off_topic")


def _resolve_lane_hint_from_semantic_frame(
    *,
    domain: str,
    taxpayer_facts: dict[str, object],
    historical_version_hint: str | None,
    tax_year_hint: int | None,
    normalized_prompt_text: str,
) -> str | None:
    category = cast(dict[str, object] | None, taxpayer_facts.get("taxpayer_category"))
    category_value = None
    if category is not None:
        raw_category_value = category.get("value")
        if isinstance(raw_category_value, str):
            stripped_category_value = raw_category_value.strip()
            if stripped_category_value:
                category_value = stripped_category_value
    if domain == "income_tax":
        if category_value == "resident_employment_plus_qualifying_interest":
            lane_descriptor = "resident employment plus qualifying interest"
        elif category_value == "resident_employment":
            lane_descriptor = "resident employment"
        elif category_value == "non_resident_employment":
            lane_descriptor = "non-resident employment"
        elif "resident employment plus qualifying interest" in normalized_prompt_text:
            lane_descriptor = "resident employment plus qualifying interest"
        elif "non-resident employment" in normalized_prompt_text or "non resident employment" in normalized_prompt_text:
            lane_descriptor = "non-resident employment"
        elif "resident employment" in normalized_prompt_text or "resident employee" in normalized_prompt_text:
            lane_descriptor = "resident employment"
        else:
            lane_descriptor = None
        if lane_descriptor is not None:
            version = historical_version_hint or (
                _INCOME_TAX_VERSION_BY_YEAR.get(tax_year_hint) if tax_year_hint is not None else None
            )
            if version is not None and tax_year_hint is not None:
                return SUPPORTED_LANE_HINTS.get((lane_descriptor, version, tax_year_hint))
    if domain == "health_contribution":
        if category_value == "sha_shif_salaried":
            lane_descriptor = "sha/shif salaried"
        elif category_value == "sha_shif_non_salaried":
            lane_descriptor = "sha/shif non-salaried"
        elif category_value == "nhif_legacy":
            lane_descriptor = "nhif legacy"
        else:
            if "sha/shif salaried" in normalized_prompt_text or "sha shif salaried" in normalized_prompt_text:
                lane_descriptor = "sha/shif salaried"
            elif "sha/shif non-salaried" in normalized_prompt_text or "sha shif non-salaried" in normalized_prompt_text:
                lane_descriptor = "sha/shif non-salaried"
            elif "nhif" in normalized_prompt_text:
                lane_descriptor = "nhif legacy"
            else:
                lane_descriptor = None
        if lane_descriptor is not None and historical_version_hint is not None and tax_year_hint is not None:
            resolved = SUPPORTED_HEALTH_LANE_HINTS.get(
                (lane_descriptor, historical_version_hint, tax_year_hint)
            )
            if resolved is not None:
                return resolved[0]
    return None


def _map_primary_goal_to_intent_class(
    *,
    primary_goal: str,
    requested_capabilities: list[str],
    requested_deliverables: list[str],
) -> str:
    if primary_goal == "compute_tax_obligation":
        if "legal_basis_explanation" in requested_capabilities:
            return "compute_plus_grounding"
        return "compute_income_tax"
    if primary_goal == "compute_statutory_contribution":
        if "legal_basis_explanation" in requested_capabilities:
            return "compute_plus_grounding"
        return "compute_health_contribution"
    if primary_goal in {"retrieve_governed_knowledge", "explain_legal_basis"}:
        return "lookup_grounded_knowledge"
    if primary_goal == "compare_tax_periods_or_regimes":
        return "lookup_grounded_knowledge"
    if primary_goal == "produce_report":
        return "generate_report_artifact"
    if primary_goal == "produce_form":
        return "generate_form_artifact"
    if primary_goal == "extract_document":
        return "extract_document"
    if primary_goal == "retrieve_artifact_information":
        return "retrieve_grounded_knowledge"
    if primary_goal == "clarify_or_correct_request":
        return "clarification_required"
    if "legal_basis_explanation" in requested_capabilities and "computation" in requested_capabilities:
        return "compute_plus_grounding"
    if "report_generation" in requested_capabilities:
        return "generate_report_artifact"
    if "form_generation" in requested_capabilities:
        return "generate_form_artifact"
    if "document_extraction" in requested_capabilities:
        return "extract_document"
    if requested_deliverables:
        return "lookup_grounded_knowledge"
    return "unknown"


def _candidate_service_families_from_capabilities(
    *,
    requested_capabilities: list[str],
    requested_deliverables: list[str],
) -> list[str]:
    families: list[str] = []
    if "computation" in requested_capabilities:
        families.append("tax_core")
    if any(item in requested_capabilities for item in ("governed_knowledge_retrieval", "legal_basis_explanation", "timeline_comparison", "artifact_detail_retrieval")):
        families.append("knowledge")
    if "form_generation" in requested_capabilities:
        families.append("forms")
    if "report_generation" in requested_capabilities:
        families.append("reports")
    if "document_extraction" in requested_capabilities:
        families.append("document_ai")
    if not families and requested_deliverables:
        families.append("knowledge")
    return list(dict.fromkeys(families))


def _knowledge_route_mode_hint_from_semantic_frame(
    *,
    requested_capabilities: list[str],
    temporal_scope: dict[str, object],
) -> str | None:
    if "timeline_comparison" in requested_capabilities or (
        temporal_scope.get("comparison_years") and len(cast(list[object], temporal_scope["comparison_years"])) >= 2
    ):
        return "timeline_search"
    if "artifact_detail_retrieval" in requested_capabilities:
        return "retrieve"
    if "governed_knowledge_retrieval" in requested_capabilities or "legal_basis_explanation" in requested_capabilities:
        return "search"
    return None
