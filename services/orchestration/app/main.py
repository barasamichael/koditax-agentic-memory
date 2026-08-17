"""Orchestration runtime boundary with deterministic tax-domain execution planning."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedFunction=false, reportUnnecessaryIsInstance=false, reportIndexIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false
import json
import queue
import time
from typing import Any
from typing import cast
from typing import Literal
from typing import Protocol
from typing import Annotated
from typing import TypedDict
import hashlib
from pathlib import Path as PathlibPath
from datetime import date
from datetime import datetime
from datetime import UTC
import threading
from collections import defaultdict
from collections.abc import Mapping
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Sequence

from dotenv import load_dotenv
from fastapi import Body
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi import APIRouter
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import PrivateAttr
from pydantic import field_validator
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import Field

from shared.authz.rbac import Principal
from shared.authz.rbac import build_authorized_principal_dependency
from shared.tracing.correlation import get_trace_id
from shared.tracing.correlation import get_correlation_id
from shared.tracing.correlation import CorrelationIdMiddleware
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeRepositoryError
from services.knowledge.app.repository import get_default_knowledge_repository
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.orchestration.app.config import OrchestrationRuntimeRolloutConfig
from services.orchestration.app.config import OrchestrationConversationContextConfig
from services.orchestration.app.config import load_orchestration_runtime_rollout_config
from services.orchestration.app.config import load_orchestration_conversation_context_config
from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.audit_events import list_income_tax_audit_events
from services.orchestration.app.audit_events import OrchestrationAuditStoreError
from services.orchestration.app.request_timer import timed_print
from services.orchestration.app.request_timer import install_request_timer
from services.validation.app.validation_rules import evaluate_orchestration_workflow_validation
from services.orchestration.app.intent_to_plan import IntentToPlanError
from services.orchestration.app.intent_to_plan import GovernedOrchestrationPlan
from services.orchestration.app.intent_to_plan import build_governed_orchestration_plan
from services.orchestration.app.intent_to_plan import translate_income_tax_intent_to_plan
from services.orchestration.app.intent_to_plan import extract_selected_route_from_governed_plan
from services.orchestration.app.fact_consistency import compare_stated_facts
from services.orchestration.app.kill_switch_guard import evaluate_income_tax_action_safety_controls
from services.orchestration.app.kill_switch_guard import (
    evaluate_orchestration_feature_safety_controls,
)
from services.orchestration.app.followup_resolution import build_bounded_candidates
from services.orchestration.app.followup_resolution import build_followup_resolution
from services.orchestration.app.followup_resolution import immediately_preceding_clarification
from services.orchestration.app.followup_resolution import FollowupResolutionResult
from services.orchestration.app.multi_step_execution import execute_governed_multi_step_plan
from services.orchestration.app.orchestration_errors import OrchestrationRuntimeError
from services.orchestration.app.orchestration_errors import build_orchestration_error_envelope
from services.orchestration.app.debug_trace import bounded_preview
from services.orchestration.app.debug_trace import emit_orchestration_debug
from services.orchestration.app.intent_plan_validator import validate_governed_orchestration_plan
from services.orchestration.app.intent_plan_validator import (
    validate_income_tax_intent_plan_for_dispatch,
)
from services.orchestration.app.llm_response_contract import AnswerMode
from services.orchestration.app.llm_response_contract import UnifiedAnswerCitationModel
from services.orchestration.app.llm_response_contract import UnifiedAnswerSourceReferenceModel
from services.orchestration.app.llm_response_contract import UnifiedAnswerResponseModel
from services.orchestration.app.llm_synthesis_context import SynthesisContextError
from services.orchestration.app.llm_synthesis_context import GovernedSynthesisContext
from services.orchestration.app.llm_synthesis_context import build_governed_synthesis_context
from services.orchestration.app.llm_synthesis_context import inject_verification_failure_reasons
from services.orchestration.app.llm_synthesis_context import requires_grounded_legal_basis_synthesis
from services.orchestration.app.action_execution_store import ActionExecutionStoreError
from services.orchestration.app.final_outcome_envelope import map_action_status_to_outcome_status
from services.orchestration.app.final_outcome_envelope import (
    build_income_tax_final_outcome_envelope,
)
from services.orchestration.app.llm_response_generator import LLMResponseStreamEvent
from services.orchestration.app.llm_response_generator import LLMResponseGenerationError
from services.orchestration.app.llm_response_generator import LLMResponseGeneratorProtocol
from services.orchestration.app.llm_response_generator import build_default_llm_response_generator
from services.orchestration.app.llm_response_generator import build_failed_unified_answer_response
from services.orchestration.app.prompt_intent_envelope import PromptIntentEnvelope
from services.orchestration.app.prompt_intent_envelope import PromptIntentEnvelopeError
from services.orchestration.app.prompt_intent_envelope import extract_knowledge_route_payload
from services.orchestration.app.prompt_intent_envelope import supports_grounded_explanation_intent
from services.orchestration.app.prompt_intent_envelope import (
    parse_income_tax_prompt_intent_envelope,
)
from services.orchestration.app.prompt_intent_envelope import build_prompt_intent_envelope_from_turn_resolution
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolver
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolutionInput
from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolutionError
from services.orchestration.app.conversation_turn_resolution import build_default_turn_resolver
from services.orchestration.app.conversation_turn_resolution import validate_conversation_turn_resolution
from services.orchestration.app.config import load_orchestration_openai_response_synthesis_config
from openai import OpenAI
from services.orchestration.app.action_adapter_registry import SUPPORTED_ROUTE_ACTIONS
from services.orchestration.app.action_adapter_registry import resolve_supported_route_action_type
from services.orchestration.app.action_adapter_registry import (
    dispatch_route_action_request_with_envelope,
)
from services.orchestration.app.pilot_tenant_guardrails import (
    evaluate_orchestration_pilot_tenant_feature,
)
from services.orchestration.app.conversation_state_store import ConversationStateStore
from services.orchestration.app.conversation_state_store import ConversationStateRecord
from services.orchestration.app.conversation_state_store import ConversationStateStoreError
from services.orchestration.app.conversation_state_store import (
    build_default_conversation_state_store,
)
from services.orchestration.app.action_execution_envelope import ActionExecutionRequest
from services.orchestration.app.action_execution_envelope import OrchestrationExecutionPlan
from services.orchestration.app.action_execution_envelope import (
    build_action_execution_request_fingerprint,
)
from services.orchestration.app.action_execution_envelope import (
    get_default_action_execution_idempotency_store,
)
from services.orchestration.app.prompt_semantic_extractor import UserContextSummary
from services.orchestration.app.prompt_semantic_extractor import ExtractedTaxpayerFacts
from services.orchestration.app.answer_verification_engine import AnswerVerificationEngine
from services.orchestration.app.income_tax_capability_gate import IncomeTaxCapabilityGateError
from services.orchestration.app.income_tax_capability_gate import (
    enforce_income_tax_runtime_capability_gate,
)
from services.orchestration.app.response_integrity_signals import FactMismatch
from services.orchestration.app.response_integrity_signals import ResponseIntegritySignals
from services.orchestration.app.conversation_context_builder import build_conversation_state_payload
from services.orchestration.app.conversation_state_protection import protect_stated_facts
from services.orchestration.app.conversation_state_protection import unprotect_stated_facts
from services.orchestration.app.conversation_state_protection import ConversationStateProtector
from services.orchestration.app.conversation_state_protection import (
    build_default_conversation_state_protector,
)
from services.orchestration.app.grounded_explanation_renderer import GroundedExplanationError
from services.orchestration.app.grounded_explanation_renderer import GroundedExplanationPayload
from services.orchestration.app.grounded_explanation_renderer import render_grounded_explanation
from services.orchestration.app.synthesis_integrity_constants import MAX_VERIFICATION_RETRIES
from services.orchestration.app.synthesis_integrity_constants import MAX_SYNTHESIS_TOOL_ITERATIONS
from shared.validation.income_tax_capability_manifest import load_income_tax_vertical_slice_manifest


class _EventHandlerApplicationProtocol(Protocol):
    def add_event_handler(self, event_type: str, func: Callable[[], object]) -> None:
        """Register a synchronous application lifecycle callback."""

        ...


load_dotenv(dotenv_path=PathlibPath(__file__).parent.parent.parent.parent / ".env")

SERVICE_NAME = "orchestration"
SERVICE_VERSION = "0.1.0"
INVALID_ORCHESTRATION_REQUEST = "invalid_orchestration_request"
UNSUPPORTED_ORCHESTRATION_SCOPE = "unsupported_orchestration_scope"
OFF_TOPIC_PROMPT = "off_topic_prompt"
UNSUPPORTED_PROMPT_SCOPE = "unsupported_prompt_scope"
UNSUPPORTED_ORCHESTRATION_ROUTE = "unsupported_orchestration_route"
INVALID_ROUTE_SELECTION = "invalid_route_selection"
REQUEST_BODY_OPTIONAL = Body(None)
ROUTER = APIRouter()
_ORCHESTRATION_RAW_STATE_ROLES = frozenset({"IndividualTaxpayer", "TaxAgent", "Accountant"})
_ORCHESTRATION_DELEGATED_ROLES = frozenset({"TaxAgent", "Accountant"})
require_orchestration_principal = build_authorized_principal_dependency(
    allowed_roles=_ORCHESTRATION_RAW_STATE_ROLES,
    allowed_delegated_roles=_ORCHESTRATION_DELEGATED_ROLES,
    required_tenant_id=None,
    allow_delegation=True,
)

SUPPORTED_ORCHESTRATION_KNOWLEDGE_DOMAINS = frozenset(
    {
        "income_tax",
        "health_contribution",
        "paye_generalized",
        "vat",
        "withholding_tax_generalized",
        "rental_income_generalized",
        "business_income_generalized",
        "general_tax",
    }
)


class KnowledgeRouteRepositoryProtocol(Protocol):
    """Describe the governed knowledge repository operations used by orchestration."""

    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]: ...

    def retrieve_records(
        self,
        *,
        source_ids: tuple[str, ...],
        anchor_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSearchRecord, ...]: ...

    def timeline_search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str,
        start_date: date,
        end_date: date,
    ) -> tuple[KnowledgeTimelineRecord, ...]: ...

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
    ) -> tuple[KnowledgeSourceVersionSummaryRecord, ...]: ...


class PromptPayload(BaseModel):
    """Represent one normalized prompt payload shape."""

    model_config = ConfigDict(extra="forbid")

    text: str
    format: Literal["plain_text"] = "plain_text"

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Prompt text must be a non-empty string.")
        if len(normalized) > 4000:
            raise ValueError("Prompt text must be at most 4000 characters.")
        return normalized


class PromptIngestionRequest(BaseModel):
    """Represent deterministic prompt-ingestion request envelope."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    conversation_id: str
    channel: Literal["chat"]
    prompt: PromptPayload

    @field_validator("tenant_id", "conversation_id")
    @classmethod
    def _validate_required_string(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value must be a non-empty string.")
        if len(normalized) > 128:
            raise ValueError("Value must be at most 128 characters.")
        return normalized


class PromptIngestionResponse(BaseModel):
    """Represent deterministic prompt-ingestion acknowledgement envelope."""

    status: Literal["accepted"]
    service: str
    correlation_id: str
    trace_id: str
    ingestion_id: str
    tenant_id: str
    conversation_id: str
    channel: Literal["chat"]
    prompt_format: Literal["plain_text"]
    prompt_checksum: str


class OrchestrationRouteSelection(BaseModel):
    """Represent deterministic orchestration route decision output."""

    route_id: str
    target_service: str
    target_operation: str


class OrchestrationPlanStepModel(BaseModel):
    """Represent one canonical orchestration plan step."""

    step_id: str
    route_id: str
    target_service: str
    target_operation: str
    step_status: str
    depends_on: list[str]
    step_purpose: str | None = None


class OrchestrationPlanModel(BaseModel):
    """Represent canonical orchestration execution plan envelope."""

    plan_id: str
    plan_version: str
    plan_status: str
    planning_mode: str
    execution_ready: bool
    steps: list[OrchestrationPlanStepModel]


class OrchestrationStepExecutionResultModel(BaseModel):
    """Represent one deterministic executed orchestration plan step result."""

    step_id: str
    route_id: str
    target_service: str
    target_operation: str
    step_status: Literal["planned", "running", "resolved", "blocked", "rejected"]
    depends_on: list[str]
    step_purpose: str | None = None
    execution_id: str | None = None
    mapped_result: dict[str, object] | None = None
    adapter_response: dict[str, object] | None = None
    error: dict[str, object] | None = None


class OrchestrationStepExecutionSummaryModel(BaseModel):
    """Represent deterministic summary counts for executed orchestration plan steps."""

    total_steps: int
    resolved_steps: int
    blocked_steps: int
    rejected_steps: int
    pending_steps: int
    accepted_steps: int


class PromptClarificationRequirement(BaseModel):
    """Represent machine-consumable clarification requirements for risky prompts."""

    reason_code: str
    message: str
    required_context_fields: list[str]
    candidate_service_families: list[str]


class GroundedKnowledgeEvidence(BaseModel):
    """Represent one governed grounded knowledge evidence item."""

    source_id: str
    source_version_id: str
    anchor_id: str
    title: str
    url: str
    source_type: str
    authority_level: str
    tax_domain: str
    effective_from: str
    effective_to: str | None = None
    tax_year: int | None = None
    publication_state: str
    source_version_form: str
    canonical_source_ref: str
    knowledge_route_mode: Literal["search", "retrieve", "timeline_search"]
    timeline_position: int | None = None
    grounding_status: Literal["grounded"]
    canonical_claims: list[dict[str, object]] | None = None


class GroundedExplanationItem(BaseModel):
    """Represent one deterministic explanation item tied to grounded evidence."""

    explanation_text: str
    source_id: str
    source_version_id: str
    anchor_id: str
    authority_level: str
    source_type: str
    temporal_applicability: str


class GroundedExplanationCitation(BaseModel):
    """Represent one deterministic citation item for grounded explanation output."""

    citation_index: int
    source_id: str
    source_version_id: str
    anchor_id: str
    title: str
    url: str
    source_type: str
    authority_level: str
    tax_domain: str
    temporal_applicability: str
    effective_from: str
    effective_to: str | None = None
    tax_year: int | None = None


class GroundedAuthoritySummary(BaseModel):
    """Represent deterministic authority summary for grounded explanation output."""

    highest_authority_level: str
    source_types: list[str]
    citation_count: int


class GroundedTemporalApplicability(BaseModel):
    """Represent deterministic temporal disclosure for grounded explanation output."""

    scope: str
    effective_from: str
    effective_to: str | None = None
    tax_year: int | None = None
    disclosure_text: str


class PromptTurnResolutionSummary(BaseModel):
    relationship: str
    operation_mode: str
    contextualized_prompt: str
    answerability: str
    assumptions: list[str]
    confidence: float


class PromptDecisionResponse(BaseModel):
    """Represent deterministic prompt decision pipeline response."""

    status: Literal["resolved", "clarification_required"]
    service: str
    correlation_id: str
    trace_id: str
    decision_id: str
    prompt_checksum: str
    intent_class: str
    tax_domain_hint: str
    supported_lane_id: str | None = None
    historical_version_id: str | None = None
    regime_identifier: str | None = None
    gate_status: Literal["allowed", "plan_only", "clarification_required"]
    selected_route: OrchestrationRouteSelection | None = None
    plan: OrchestrationPlanModel
    clarification: PromptClarificationRequirement | None = None
    turn_resolution: PromptTurnResolutionSummary | None = None


class ActionSafetyContext(BaseModel):
    """Represent deterministic action-safety context required for unsafe paths."""

    model_config = ConfigDict(extra="forbid")

    risk_class: Literal["low", "high"] = "low"
    confirmation_state: Literal["confirmed", "pending", "unknown"] = "unknown"
    step_up_proof_state: Literal["bound", "unbound", "not_required"] = "not_required"


class PromptExecutionRequest(PromptIngestionRequest):
    """Represent deterministic prompt execution request for route-selected adapter dispatch."""

    idempotency_key: str
    intent_class: str
    tax_domain_hint: str
    decision_id: str
    selected_route: OrchestrationRouteSelection | None = None
    action_context: ActionSafetyContext | None = None
    _effective_taxpayer_user_id: str | None = PrivateAttr(default=None)

    @property
    def effective_taxpayer_user_id(self) -> str:
        """Return the owner derived from the validated auth context only."""

        if self._effective_taxpayer_user_id is None:
            raise RuntimeError("Trusted conversation owner has not been resolved.")
        return self._effective_taxpayer_user_id

    def bind_effective_taxpayer_user_id(self, user_id: str) -> None:
        """Bind the validated effective taxpayer owner for internal execution use."""

        self._effective_taxpayer_user_id = user_id

    @field_validator("idempotency_key", "intent_class", "tax_domain_hint")
    @classmethod
    def _validate_non_empty_short_string(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value must be a non-empty string.")
        if len(normalized) > 128:
            raise ValueError("Value must be at most 128 characters.")
        return normalized

    @field_validator("decision_id")
    @classmethod
    def _validate_decision_id(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) != 64:
            raise ValueError("Decision id must be 64 hexadecimal characters.")
        if any(ch not in "0123456789abcdef" for ch in normalized.lower()):
            raise ValueError("Decision id must be a lowercase hexadecimal digest.")
        return normalized.lower()


class ConversationDeleteResponse(BaseModel):
    """Represent deterministic conversation deletion acknowledgement."""

    status: Literal["deleted"]
    service: str
    correlation_id: str
    trace_id: str
    conversation_id: str
    deleted_count: int


class ConversationRenameRequest(BaseModel):
    """Represent deterministic conversation rename request."""

    model_config = ConfigDict(extra="forbid")

    conversation_title: str

    @field_validator("conversation_title")
    @classmethod
    def _validate_conversation_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Conversation title must be a non-empty string.")
        if len(normalized) > 80:
            raise ValueError("Conversation title must be at most 80 characters.")
        return normalized


class ConversationRenameResponse(BaseModel):
    """Represent deterministic conversation rename acknowledgement."""

    status: Literal["renamed"]
    service: str
    correlation_id: str
    trace_id: str
    conversation_id: str
    conversation_title: str
    updated_count: int


class ConversationHistoryMessage(BaseModel):
    """Represent one browser-visible conversation message."""

    id: str
    role: Literal["user", "assistant"]
    content: str
    timestamp: str
    type: Literal["text", "action_approval", "outcome", "error"]
    metadata: dict[str, object] | None = None


class ConversationHistoryConversation(BaseModel):
    """Represent one browser-visible conversation thread."""

    conversation_id: str
    title: str
    created_at: str
    updated_at: str
    status: Literal["draft", "active", "attention"]
    messages: list[ConversationHistoryMessage]


class ConversationHistoryListResponse(BaseModel):
    """Represent deterministic browser conversation hydration payload."""

    status: Literal["listed"]
    service: str
    correlation_id: str
    trace_id: str
    conversations: list[ConversationHistoryConversation]


class BulkConversationDeleteRequest(BaseModel):
    """Represent deterministic bulk conversation deletion request."""

    model_config = ConfigDict(extra="forbid")

    conversation_ids: list[str]

    @field_validator("conversation_ids")
    @classmethod
    def _validate_conversation_ids(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("At least one conversation id is required.")
        deduped: list[str] = []
        seen: set[str] = set()
        for raw_value in value:
            normalized = raw_value.strip()
            if not normalized:
                raise ValueError("Conversation id must be a non-empty string.")
            if len(normalized) > 128:
                raise ValueError("Conversation id must be at most 128 characters.")
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped


class BulkConversationDeleteResponse(BaseModel):
    """Represent deterministic bulk conversation deletion acknowledgement."""

    status: Literal["deleted"]
    service: str
    correlation_id: str
    trace_id: str
    requested_conversation_ids: list[str]
    deleted_conversation_ids: list[str]
    deleted_count: int


class TrustedConversationOwner(TypedDict):
    """Represent the tenant and taxpayer owner derived from trusted context."""

    tenant_id: str
    effective_taxpayer_user_id: str
    role: str
    delegation_id: str | None


def _conversation_record_timestamp(record: ConversationStateRecord) -> str:
    created_at = record.get("created_at")
    if isinstance(created_at, str) and created_at.strip():
        return created_at
    updated_at = record.get("updated_at")
    if isinstance(updated_at, str) and updated_at.strip():
        return updated_at
    return ""


def _build_conversation_history_message(
    *,
    record: ConversationStateRecord,
    role: Literal["user", "assistant"],
    content: str,
    message_type: Literal["text", "action_approval", "outcome", "error"],
    suffix: str,
    metadata: dict[str, object] | None = None,
) -> ConversationHistoryMessage | None:
    normalized_content = content.strip()
    if not normalized_content:
        return None
    timestamp = _conversation_record_timestamp(record)
    if not timestamp:
        timestamp = datetime.now(UTC).isoformat()
    return ConversationHistoryMessage(
        id=f"{record['execution_id']}:{suffix}",
        role=role,
        content=normalized_content,
        timestamp=timestamp,
        type=message_type,
        metadata=metadata,
    )


def _build_conversation_history_conversation(
    conversation_id: str,
    records: Sequence[ConversationStateRecord],
) -> ConversationHistoryConversation | None:
    ordered_records = sorted(
        records,
        key=lambda record: (
            _conversation_record_timestamp(record),
            record["execution_id"],
        ),
    )
    if not ordered_records:
        return None

    messages: list[ConversationHistoryMessage] = []
    title = "New chat"
    created_at = _conversation_record_timestamp(ordered_records[0]) or ""
    updated_at = _conversation_record_timestamp(ordered_records[-1]) or created_at
    status: Literal["draft", "active", "attention"] = "draft"

    for record in ordered_records:
        context = record["context_payload"]
        record_title = context.get("conversation_title")
        if isinstance(record_title, str) and record_title.strip():
            title = record_title.strip()

        prompt_text = context.get("raw_prompt_text") or context.get("prompt_text")
        if isinstance(prompt_text, str):
            user_message = _build_conversation_history_message(
                record=record,
                role="user",
                content=prompt_text,
                message_type="text",
                suffix="user",
            )
            if user_message is not None:
                messages.append(user_message)

        assistant_text = context.get("assistant_answer_text")
        if not isinstance(assistant_text, str) or not assistant_text.strip():
            assistant_text = context.get("assistant_answer_summary")
        if not isinstance(assistant_text, str) or not assistant_text.strip():
            assistant_text = context.get("clarification_question")
        assistant_message_type: Literal["text", "action_approval", "outcome", "error"] = "outcome"
        assistant_state = "completed"
        turn_outcome_kind = context.get("turn_outcome_kind")
        if isinstance(turn_outcome_kind, str) and turn_outcome_kind in {
            "execution_failed",
            "system_failure",
            "failure",
        }:
            assistant_message_type = "error"
            assistant_state = "failed"
            if not isinstance(assistant_text, str) or not assistant_text.strip():
                assistant_text = context.get("failure_summary") or context.get("user_facing_message")
        elif context.get("assistant_turn_kind") == "clarification":
            assistant_message_type = "text"

        if isinstance(assistant_text, str):
            assistant_message = _build_conversation_history_message(
                record=record,
                role="assistant",
                content=assistant_text,
                message_type=assistant_message_type,
                suffix="assistant",
                metadata={"assistantState": assistant_state},
            )
            if assistant_message is not None:
                messages.append(assistant_message)

        if any(
            message.type == "error"
            or (message.metadata is not None and message.metadata.get("assistantState") == "failed")
            for message in messages
        ):
            status = "attention"
        elif messages:
            status = "active"

    return ConversationHistoryConversation(
        conversation_id=conversation_id,
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        status=status,
        messages=messages,
    )


def _resolve_trusted_conversation_owner(
    *,
    request: Request,
    payload: PromptIngestionRequest,
    principal: Principal,
) -> TrustedConversationOwner:
    """Derive the only owner permitted to read or write conversation state."""

    if payload.tenant_id != principal.tenant_id:
        raise _http_error(
            request=request,
            status_code=403,
            error_code="authorization_tenant_forbidden",
            message="Requested tenant does not match authenticated tenant.",
            reason="authorization_tenant_forbidden",
            reason_code="authorization_tenant_forbidden",
            context={"tenant_id": payload.tenant_id},
        )
    delegation = principal.delegation_context
    if principal.role in _ORCHESTRATION_DELEGATED_ROLES:
        if not delegation.is_delegated or delegation.principal_user_id is None:
            raise _http_error(
                request=request,
                status_code=403,
                error_code="authorization_delegation_required",
                message="Delegated taxpayer authorization is required.",
                reason="authorization_delegation_required",
                reason_code="authorization_delegation_required",
                context={"role": principal.role},
            )
        owner = str(delegation.principal_user_id)
        delegation_id = str(delegation.delegation_id) if delegation.delegation_id else None
    else:
        owner = str(principal.user_id)
        delegation_id = None
    request.state.effective_taxpayer_user_id = owner
    request.state.authorization_role = principal.role
    request.state.authorization_delegation_id = delegation_id
    return {
        "tenant_id": principal.tenant_id,
        "effective_taxpayer_user_id": owner,
        "role": principal.role,
        "delegation_id": delegation_id,
    }


def _resolve_trusted_owner_from_principal(
    *,
    request: Request,
    principal: Principal,
) -> TrustedConversationOwner:
    """Derive the authenticated conversation owner for delete operations."""

    delegation = principal.delegation_context
    if principal.role in _ORCHESTRATION_DELEGATED_ROLES:
        if not delegation.is_delegated or delegation.principal_user_id is None:
            raise _http_error(
                request=request,
                status_code=403,
                error_code="authorization_delegation_required",
                message="Delegated taxpayer authorization is required.",
                reason="authorization_delegation_required",
                reason_code="authorization_delegation_required",
                context={"role": principal.role},
            )
        owner = str(delegation.principal_user_id)
        delegation_id = str(delegation.delegation_id) if delegation.delegation_id else None
    else:
        owner = str(principal.user_id)
        delegation_id = None
    request.state.effective_taxpayer_user_id = owner
    request.state.authorization_role = principal.role
    request.state.authorization_delegation_id = delegation_id
    return {
        "tenant_id": principal.tenant_id,
        "effective_taxpayer_user_id": owner,
        "role": principal.role,
        "delegation_id": delegation_id,
    }


class PromptExecutionResponse(BaseModel):
    """Represent deterministic route execution response envelope."""

    status: Literal["executed"]
    service: str
    correlation_id: str
    trace_id: str
    execution_id: str
    decision_id: str
    prompt_checksum: str
    tax_domain_hint: str
    supported_lane_id: str | None = None
    historical_version_id: str | None = None
    regime_identifier: str | None = None
    plan: OrchestrationPlanModel
    grounding_status: Literal["grounded", "not_applicable"] | None = None
    grounded_evidence: list[GroundedKnowledgeEvidence] | None = None
    explanation_status: Literal["grounded", "not_applicable"] | None = None
    explanation_items: list[GroundedExplanationItem] | None = None
    citations: list[GroundedExplanationCitation] | None = None
    source_references: list[UnifiedAnswerSourceReferenceModel] = Field(default_factory=list)
    authority_summary: GroundedAuthoritySummary | None = None
    temporal_applicability: GroundedTemporalApplicability | None = None
    selected_route: OrchestrationRouteSelection | None = None
    step_results: list[OrchestrationStepExecutionResultModel] | None = None
    step_summary: OrchestrationStepExecutionSummaryModel | None = None
    execution_status: Literal["resolved"]
    mapped_result: dict[str, object]
    adapter_response: dict[str, object] | None = None
    validation: dict[str, object] | None = None
    response: UnifiedAnswerResponseModel
    final_outcome: dict[str, object]
    errors: list[dict[str, object]] | None = None


_STREAM_END = object()


class _StreamingLLMResponseGeneratorProxy:
    """Bridge sync orchestration execution with streamed OpenAI answer deltas."""

    def __init__(
        self,
        *,
        base_generator: LLMResponseGeneratorProtocol,
        event_queue: "queue.Queue[str | object]",
    ) -> None:
        self._base_generator = base_generator
        self._event_queue = event_queue

    def generate(
        self,
        context: GovernedSynthesisContext,
    ) -> UnifiedAnswerResponseModel:
        completed_response: UnifiedAnswerResponseModel | None = None
        for event in self._base_generator.stream_generate(context):
            if event.event_type == "delta" and event.delta:
                self._event_queue.put(_format_sse_event("delta", {"delta": event.delta}))
                continue
            if event.event_type == "completed" and event.response is not None:
                completed_response = event.response
        if completed_response is None:
            raise LLMResponseGenerationError(
                error_code="response_synthesis_failed",
                message=(
                    "OpenAI response synthesis completed without a final governed answer payload."
                ),
                reason_code="malformed_model_response",
            )
        return completed_response

    def stream_generate(
        self,
        context: GovernedSynthesisContext,
    ) -> Iterator[LLMResponseStreamEvent]:
        return self._base_generator.stream_generate(context)


class FollowupContextExecutionResult(TypedDict):
    """Represent one synthetic execution result built from prior conversation context."""

    execution_id: str
    plan: dict[str, object]
    mapped_result: dict[str, object]
    adapter_response: dict[str, object]


PRIMARY_ROUTE_BY_INTENT: dict[str, OrchestrationRouteSelection] = {
    "compute_income_tax": OrchestrationRouteSelection(
        route_id="income_tax_compute_route_v1",
        target_service="tax_core",
        target_operation="execute_computation",
    ),
    "compute_health_contribution": OrchestrationRouteSelection(
        route_id="health_contribution_compute_route_v1",
        target_service="tax_core",
        target_operation="execute_computation",
    ),
    "lookup_grounded_knowledge": OrchestrationRouteSelection(
        route_id="knowledge_search_route_v1",
        target_service="knowledge",
        target_operation="search_knowledge",
    ),
    "retrieve_grounded_knowledge": OrchestrationRouteSelection(
        route_id="knowledge_retrieve_route_v1",
        target_service="knowledge",
        target_operation="retrieve_knowledge",
    ),
    "timeline_grounded_knowledge": OrchestrationRouteSelection(
        route_id="knowledge_timeline_route_v1",
        target_service="knowledge",
        target_operation="timeline_search_knowledge",
    ),
    "meta_conversation": OrchestrationRouteSelection(
        route_id="meta_conversation_route_v1",
        target_service="orchestration",
        target_operation="generate_meta_conversation_response",
    ),
}

ALLOWED_EXECUTION_ROUTES_BY_DOMAIN: dict[str, dict[str, OrchestrationRouteSelection]] = {
    "income_tax": {
        "income_tax_compute_route_v1": OrchestrationRouteSelection(
            route_id="income_tax_compute_route_v1",
            target_service="tax_core",
            target_operation="execute_computation",
        ),
        "income_tax_form_generation_route_v1": OrchestrationRouteSelection(
            route_id="income_tax_form_generation_route_v1",
            target_service="forms",
            target_operation="generate_income_tax_form_artifact",
        ),
        "income_tax_report_generation_route_v1": OrchestrationRouteSelection(
            route_id="income_tax_report_generation_route_v1",
            target_service="reports",
            target_operation="create_income_tax_report_artifact",
        ),
        "income_tax_document_evidence_route_v1": OrchestrationRouteSelection(
            route_id="income_tax_document_evidence_route_v1",
            target_service="document_ai",
            target_operation="search_document_evidence",
        ),
        "knowledge_search_route_v1": OrchestrationRouteSelection(
            route_id="knowledge_search_route_v1",
            target_service="knowledge",
            target_operation="search_knowledge",
        ),
        "knowledge_retrieve_route_v1": OrchestrationRouteSelection(
            route_id="knowledge_retrieve_route_v1",
            target_service="knowledge",
            target_operation="retrieve_knowledge",
        ),
        "knowledge_timeline_route_v1": OrchestrationRouteSelection(
            route_id="knowledge_timeline_route_v1",
            target_service="knowledge",
            target_operation="timeline_search_knowledge",
        ),
    },
    "health_contribution": {
        "health_contribution_compute_route_v1": OrchestrationRouteSelection(
            route_id="health_contribution_compute_route_v1",
            target_service="tax_core",
            target_operation="execute_computation",
        ),
        "health_contribution_form_mapping_route_v1": OrchestrationRouteSelection(
            route_id="health_contribution_form_mapping_route_v1",
            target_service="forms",
            target_operation="map_health_contribution_output_to_form_ready",
        ),
        "health_contribution_report_generation_route_v1": OrchestrationRouteSelection(
            route_id="health_contribution_report_generation_route_v1",
            target_service="reports",
            target_operation="create_health_contribution_report_artifact",
        ),
        "knowledge_search_route_v1": OrchestrationRouteSelection(
            route_id="knowledge_search_route_v1",
            target_service="knowledge",
            target_operation="search_knowledge",
        ),
        "knowledge_retrieve_route_v1": OrchestrationRouteSelection(
            route_id="knowledge_retrieve_route_v1",
            target_service="knowledge",
            target_operation="retrieve_knowledge",
        ),
        "knowledge_timeline_route_v1": OrchestrationRouteSelection(
            route_id="knowledge_timeline_route_v1",
            target_service="knowledge",
            target_operation="timeline_search_knowledge",
        ),
    },
}

# All knowledge-only domains share the same two execution routes.
_KNOWLEDGE_ONLY_EXECUTION_ROUTES: dict[str, OrchestrationRouteSelection] = {
    "knowledge_search_route_v1": OrchestrationRouteSelection(
        route_id="knowledge_search_route_v1",
        target_service="knowledge",
        target_operation="search_knowledge",
    ),
    "knowledge_retrieve_route_v1": OrchestrationRouteSelection(
        route_id="knowledge_retrieve_route_v1",
        target_service="knowledge",
        target_operation="retrieve_knowledge",
    ),
    "knowledge_timeline_route_v1": OrchestrationRouteSelection(
        route_id="knowledge_timeline_route_v1",
        target_service="knowledge",
        target_operation="timeline_search_knowledge",
    ),
}
for _knowledge_domain in (
    "paye_generalized",
    "vat",
    "withholding_tax_generalized",
    "rental_income_generalized",
    "business_income_generalized",
    "general_tax",
):
    ALLOWED_EXECUTION_ROUTES_BY_DOMAIN[_knowledge_domain] = _KNOWLEDGE_ONLY_EXECUTION_ROUTES
ALLOWED_EXECUTION_ROUTES_BY_DOMAIN["general_tax"] = {
    **_KNOWLEDGE_ONLY_EXECUTION_ROUTES,
    "meta_conversation_route_v1": PRIMARY_ROUTE_BY_INTENT["meta_conversation"],
}

SUPPORTED_HEALTH_ROUTING_CONTEXTS: dict[tuple[str, str], str] = {
    (
        "nhif_legacy",
        "HCH-VER-20100716-A",
    ): "health_contribution_nhif_legacy_v1_2010_07_16",
    (
        "nhif_legacy",
        "HCH-VER-20150401-A",
    ): "health_contribution_nhif_legacy_v1_2015_04_01",
    (
        "nhif_legacy",
        "HCH-VER-20210528-A",
    ): "health_contribution_nhif_legacy_v1_2021_05_28",
    (
        "nhif_legacy",
        "HCH-VER-20221231-REG",
    ): "health_contribution_nhif_legacy_v1_2022_12_31_reg",
    (
        "sha_shif",
        "HCH-VER-20241001-A",
    ): "health_contribution_sha_shif_v1_2024_10_01",
    (
        "sha_shif",
        "HCH-VER-20250228-PIT",
    ): "health_contribution_sha_shif_v1_2025_02_28_pit",
    (
        "transition_boundary",
        "HCH-VER-20221231-REG",
    ): "health_contribution_nhif_legacy_v1_2022_12_31_reg",
    (
        "transition_boundary",
        "HCH-VER-20241001-A",
    ): "health_contribution_sha_shif_v1_2024_10_01",
    (
        "transition_boundary",
        "HCH-VER-20250228-PIT",
    ): "health_contribution_sha_shif_v1_2025_02_28_pit",
}

KNOWN_NON_READY_HEALTH_WINDOWS = frozenset(
    {
        "HCH-VER-20031205-A",
        "HCH-VER-20141208-A",
        "HCH-VER-20210330-A",
        "HCH-VER-20221231-ACT",
        "HCH-VER-20231122-REPEAL",
        "HCH-VER-20231122-SHIACT",
        "HCH-VER-20240308-A",
        "HCH-VER-20240701-A",
        "HCH-VER-20240920-AMD",
        "HCH-VER-20240920-PIT",
        "HCH-VER-20250228-AMD",
    }
)


def create_app(
    *,
    knowledge_repository: KnowledgeRouteRepositoryProtocol | None = None,
    llm_response_generator: LLMResponseGeneratorProtocol | None = None,
    conversation_state_store: ConversationStateStore | None = None,
    conversation_state_protector: ConversationStateProtector | None = None,
    runtime_rollout_config: OrchestrationRuntimeRolloutConfig | None = None,
    turn_resolver: ConversationTurnResolver | None = None,
) -> FastAPI:
    """Build deterministic orchestration runtime app."""

    app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION)
    app.state.knowledge_repository = knowledge_repository
    app.state.llm_response_generator = (
        llm_response_generator
        if llm_response_generator is not None
        else build_default_llm_response_generator(
            knowledge_repository_provider=lambda: _get_knowledge_repository_from_app(app)
        )
    )
    app.state.conversation_state_store = (
        conversation_state_store
        if conversation_state_store is not None
        else build_default_conversation_state_store()
    )
    app.state.conversation_context_config = load_orchestration_conversation_context_config()
    if turn_resolver is not None:
        app.state.turn_resolver = turn_resolver
    else:
        resolver_config = load_orchestration_openai_response_synthesis_config()
        if resolver_config.configured:
            app.state.turn_resolver = build_default_turn_resolver(
                client=OpenAI(api_key=resolver_config.api_key, base_url=resolver_config.base_url, timeout=resolver_config.timeout_seconds),
                model=cast(str, resolver_config.model),
            )
        else:
            raise ConversationTurnResolutionError(
                error_code="conversation_turn_resolver_not_configured",
                reason_code="conversation_turn_resolver_not_configured",
                message="Conversation turn resolver configuration is required.",
            )
    app.state.conversation_state_protector = conversation_state_protector
    app.state.runtime_rollout_config = (
        runtime_rollout_config
        if runtime_rollout_config is not None
        else load_orchestration_runtime_rollout_config()
    )
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: _get_conversation_state_store_from_app(app).purge_expired(),
        trigger=IntervalTrigger(days=1),
        id="orchestration-conversation-state-purge",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    app.state.conversation_state_scheduler = scheduler
    lifecycle_app = cast(_EventHandlerApplicationProtocol, app)
    lifecycle_app.add_event_handler("startup", scheduler.start)
    lifecycle_app.add_event_handler("shutdown", lambda: scheduler.shutdown(wait=False))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5174",
            "http://localhost:5174",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.add_middleware(CorrelationIdMiddleware)
    app.add_exception_handler(
        RequestValidationError,
        cast(Any, _handle_request_validation_error),
    )
    app.add_exception_handler(
        OrchestrationRuntimeError,
        cast(Any, _handle_orchestration_runtime_error),
    )
    app.add_exception_handler(
        OrchestrationAuditStoreError,
        cast(Any, _handle_orchestration_audit_store_error),
    )
    app.add_exception_handler(HTTPException, cast(Any, _handle_http_exception_error))
    app.add_exception_handler(
        StarletteHTTPException,
        cast(Any, _handle_starlette_http_exception_error),
    )
    app.include_router(ROUTER)
    return app


@ROUTER.get("/healthz")
def orchestration_healthz(request: Request) -> dict[str, str]:
    """Expose deterministic orchestration health endpoint."""

    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
    }


@ROUTER.get("/readyz")
def orchestration_readyz(request: Request) -> dict[str, object]:
    """Expose deterministic orchestration readiness endpoint."""

    rollout_config = _get_runtime_rollout_config(request)
    return {
        "status": "ready",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
        "response_synthesis_enabled": rollout_config.response_synthesis_enabled,
        "conversation_continuity_enabled": rollout_config.conversation_continuity_enabled,
        "release_gate_surface": "internal_helper_only",
    }


@ROUTER.post("/v1/orchestration/income-tax/execute")
def execute_income_tax_orchestration(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> dict[str, object]:
    """Build deterministic orchestration execution-plan result for one prompt."""

    timed_print("We are now at execute")
    source = _as_object(payload)
    prompt_text = _required_string(source, "prompt_text")
    tenant_id = _optional_string(source.get("tenant_id")) or "pilot_tenant_alpha"
    try:
        timed_print("We are now parsing the intent envelope...")
        user_context_for_extraction: UserContextSummary | None = None
        if hasattr(request, "state") and hasattr(request.state, "user_id"):
            user_context_for_extraction = UserContextSummary(
                user_id=str(request.state.user_id),
                tenant_id=tenant_id,
                employment_type=_optional_string(getattr(request.state, "employment_type", None)),
                filing_status=_optional_string(getattr(request.state, "filing_status", None)),
                country=_optional_string(getattr(request.state, "country", None)),
                jurisdiction=_optional_string(getattr(request.state, "jurisdiction", None)),
            )

        intent_envelope = parse_income_tax_prompt_intent_envelope(
            prompt_text,
            user_context=user_context_for_extraction,
            conversation_history=None,
            current_tax_year=2026,
        )
        enforce_income_tax_runtime_capability_gate(
            prompt_text=prompt_text,
            supported_lane_id=intent_envelope["requested_lane_hint"],
            historical_version_id=intent_envelope["historical_version_hint"],
            tax_year=intent_envelope["tax_year_hint"],
            correlation_id=intent_envelope["correlation_id"],
            tenant_id=tenant_id,
        )
        plan = translate_income_tax_intent_to_plan(intent_envelope)
        plan_validation = validate_income_tax_intent_plan_for_dispatch(plan)
        if plan_validation["validation_status"] != "accepted":
            validation_error = cast(dict[str, object], plan_validation["error"] or {})
            raise _http_error(
                request=request,
                status_code=400,
                error_code=str(validation_error.get("error_code", "unsupported_prompt_scope")),
                message=str(validation_error.get("message", "Orchestration plan rejected.")),
                reason=str(validation_error.get("reason", "unsupported_prompt_scope")),
                reason_code=str(validation_error.get("reason", "unsupported_prompt_scope")),
                context=cast(
                    dict[str, object],
                    validation_error.get("rejected_context", {}),
                ),
            )
    except PromptIntentEnvelopeError as error:
        payload_detail = error.payload()
        raise _http_error(
            request=request,
            status_code=400,
            error_code=str(payload_detail["error_code"]),
            message=str(payload_detail["message"]),
            reason=str(payload_detail["reason"]),
            reason_code=str(payload_detail["reason"]),
            context=cast(dict[str, object], payload_detail.get("rejected_context", {})),
        ) from error
    except IncomeTaxCapabilityGateError as error:
        payload_detail = error.payload()
        reason_code = str(payload_detail.get("reason_code", payload_detail["reason"]))
        status_code = (
            403 if str(payload_detail["error_code"]) == "pilot_tenant_not_allowed" else 404
        )
        raise _http_error(
            request=request,
            status_code=status_code,
            error_code=str(payload_detail["error_code"]),
            message=str(payload_detail["message"]),
            reason=str(payload_detail["reason"]),
            reason_code=reason_code,
            context=cast(dict[str, object], payload_detail.get("rejected_context", {})),
        ) from error
    except IntentToPlanError as error:
        payload_detail = error.payload()
        raise _http_error(
            request=request,
            status_code=400,
            error_code=str(payload_detail["error_code"]),
            message=str(payload_detail["message"]),
            reason=str(payload_detail["reason"]),
            reason_code=str(payload_detail["reason"]),
            context=cast(dict[str, object], payload_detail.get("rejected_context", {})),
        ) from error

    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "correlation_id": intent_envelope["correlation_id"],
        "trace_id": intent_envelope["trace_id"],
        "result": {
            "intent_class": intent_envelope["intent_class"],
            "supported_lane_id": intent_envelope["requested_lane_hint"],
            "historical_version_id": intent_envelope["historical_version_hint"],
            "tax_year": intent_envelope["tax_year_hint"],
            "plan_id": plan["plan_id"],
            "plan_status": plan["plan_status"],
            "validation_status": plan_validation["validation_status"],
        },
    }


@ROUTER.post("/v1/orchestration/prompt/ingest", response_model=PromptIngestionResponse)
def ingest_orchestration_prompt(
    request: Request,
    payload: PromptIngestionRequest,
) -> PromptIngestionResponse:
    """Accept deterministic prompt-ingestion payload without execution orchestration."""
    prompt_checksum = _compute_prompt_checksum(
        tenant_id=payload.tenant_id,
        conversation_id=payload.conversation_id,
        channel=payload.channel,
        prompt_text=payload.prompt.text,
    )
    emit_income_tax_audit_event(
        event_type="prompt_ingested",
        status="accepted",
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        context={
            "tenant_id": payload.tenant_id,
            "resource_id": prompt_checksum,
            "conversation_id": payload.conversation_id,
            "channel": payload.channel,
            "prompt_format": payload.prompt.format,
            "prompt_checksum": prompt_checksum,
        },
    )
    return PromptIngestionResponse(
        status="accepted",
        service=SERVICE_NAME,
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        ingestion_id=prompt_checksum,
        tenant_id=payload.tenant_id,
        conversation_id=payload.conversation_id,
        channel=payload.channel,
        prompt_format=payload.prompt.format,
        prompt_checksum=prompt_checksum,
    )


@ROUTER.post("/v1/orchestration/prompt/decide", response_model=PromptDecisionResponse)
def decide_orchestration_prompt_route(
    request: Request,
    payload: PromptIngestionRequest,
    principal: Annotated[Principal, Depends(require_orchestration_principal)],
) -> PromptDecisionResponse:
    """Run deterministic intent->gate->route decision pipeline for prompt ingress."""
    owner = _resolve_trusted_conversation_owner(
        request=request, payload=payload, principal=principal
    )
    resolution = _resolve_prompt_route_decision(request=request, payload=payload, owner=owner)
    if resolution.get("status") == "clarification_required":
        _persist_clarification_turn(request=request, payload=payload, owner=owner, resolution=resolution)
    # Cache the resolution so /execute can reuse it without a second LLM call.
    checksum = str(resolution.get("prompt_checksum", ""))
    if checksum:
        _cache_put_resolution(
            _owner_scoped_resolution_key(checksum=checksum, owner=owner), resolution
        )
        timed_print(f"[CACHE] decide: stored resolution for checksum={checksum[:12]}…")
    timed_print("We have a resolution...")
    resolution_map = resolution
    decision_id = resolution_map.get("decision_id")
    prompt_checksum = resolution_map.get("prompt_checksum")
    intent_class = resolution_map.get("intent_class")
    tax_domain_hint = resolution_map.get("tax_domain_hint")
    selected_route = resolution_map.get("selected_route")
    gate_status = resolution_map.get("gate_status")
    plan = resolution_map.get("plan")
    clarification = resolution_map.get("clarification")
    assert isinstance(decision_id, str)
    assert isinstance(prompt_checksum, str)
    assert isinstance(intent_class, str)
    assert isinstance(tax_domain_hint, str)
    assert isinstance(gate_status, str)
    selected_route_value = (
        cast(OrchestrationRouteSelection, selected_route) if selected_route is not None else None
    )
    assert isinstance(plan, dict)
    plan_map = cast(dict[str, object], plan)
    emit_income_tax_audit_event(
        event_type="prompt_decision_resolved",
        status=cast(str, resolution_map["status"]),
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        supported_lane_id=cast(str | None, resolution_map.get("supported_lane_id")),
        historical_version_id=cast(str | None, resolution_map.get("historical_version_id")),
        tax_year=cast(int | None, resolution_map.get("tax_year")),
        context={
            "tenant_id": payload.tenant_id,
            "authorization_outcome": "allowed",
            "authorization_role": owner["role"],
            "effective_taxpayer_owner_ref": owner["effective_taxpayer_user_id"],
            "delegation_id": owner["delegation_id"],
            "resource_id": decision_id,
            "conversation_id": payload.conversation_id,
            "intent_class": intent_class,
            "tax_domain_hint": tax_domain_hint,
            "gate_status": gate_status,
            "route_id": (
                selected_route_value.route_id if selected_route_value is not None else None
            ),
            "target_service": (
                selected_route_value.target_service if selected_route_value is not None else None
            ),
            "target_operation": (
                selected_route_value.target_operation if selected_route_value is not None else None
            ),
            "prompt_checksum": prompt_checksum,
            "plan_id": cast(str | None, plan_map.get("plan_id")),
        },
    )
    return PromptDecisionResponse(
        status=cast(
            Literal["resolved", "clarification_required"],
            resolution_map["status"],
        ),
        service=SERVICE_NAME,
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        decision_id=decision_id,
        prompt_checksum=prompt_checksum,
        intent_class=intent_class,
        tax_domain_hint=tax_domain_hint,
        supported_lane_id=cast(str | None, resolution_map.get("supported_lane_id")),
        historical_version_id=cast(str | None, resolution_map.get("historical_version_id")),
        regime_identifier=cast(str | None, resolution_map.get("regime_identifier")),
        gate_status=cast(
            Literal["allowed", "plan_only", "clarification_required"],
            gate_status,
        ),
        selected_route=selected_route_value,
        plan=OrchestrationPlanModel.model_validate(plan_map),
        clarification=(
            PromptClarificationRequirement.model_validate(clarification)
            if clarification is not None
            else None
        ),
        turn_resolution=(
            PromptTurnResolutionSummary.model_validate({key: value for key, value in cast(Mapping[str, object], resolution_map["turn_resolution"]).items() if key in {"relationship", "operation_mode", "contextualized_prompt", "answerability", "assumptions", "confidence"}})
            if isinstance(resolution_map.get("turn_resolution"), Mapping)
            else None
        ),
    )


@ROUTER.post("/v1/orchestration/prompt/execute", response_model=PromptExecutionResponse)
def execute_orchestration_prompt_route(
    request: Request,
    payload: PromptExecutionRequest,
    principal: Annotated[Principal, Depends(require_orchestration_principal)],
) -> PromptExecutionResponse:
    """Execute deterministic adapter dispatch for one selected orchestration route."""
    owner = _resolve_trusted_conversation_owner(
        request=request,
        payload=payload,
        principal=principal,
    )
    payload.bind_effective_taxpayer_user_id(owner["effective_taxpayer_user_id"])
    prompt_checksum = _compute_prompt_checksum(
        tenant_id=payload.tenant_id,
        conversation_id=payload.conversation_id,
        channel=payload.channel,
        prompt_text=payload.prompt.text,
    )
    emit_orchestration_debug(
        "RUNTIME",
        "execution.start",
        prompt_checksum=prompt_checksum,
        decision_id=payload.decision_id,
        intent_class=payload.intent_class,
        tax_domain_hint=payload.tax_domain_hint,
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
    )
    _raise_for_existing_execution_idempotency_conflict(
        request=request,
        payload=payload,
        prompt_checksum=prompt_checksum,
        route_payload=None,
    )
    # Reuse the resolution from /decide when available — avoids a second LLM call.
    install_request_timer()
    cached_resolution = _cache_get_resolution(
        _owner_scoped_resolution_key(checksum=prompt_checksum, owner=owner)
    )
    if cached_resolution is not None:
        timed_print(
            "[CACHE] execute: cache HIT for checksum="
            f"{prompt_checksum[:12]}… — skipping LLM envelope"
        )
        emit_orchestration_debug(
            "RUNTIME",
            "execution.cache.hit",
            prompt_checksum=prompt_checksum,
            decision_id=payload.decision_id,
            correlation_id=get_correlation_id(request),
            trace_id=get_trace_id(request),
        )
        resolution = cached_resolution
    else:
        timed_print(
            "[CACHE] execute: cache MISS for checksum="
            f"{prompt_checksum[:12]}… — running full envelope"
        )
        emit_orchestration_debug(
            "RUNTIME",
            "execution.cache.miss",
            prompt_checksum=prompt_checksum,
            decision_id=payload.decision_id,
            correlation_id=get_correlation_id(request),
            trace_id=get_trace_id(request),
        )
        resolution = _resolve_prompt_route_decision(request=request, payload=payload, owner=owner)
    expected_decision_id = str(resolution["decision_id"])
    expected_intent_class = str(resolution["intent_class"])
    expected_tax_domain_hint = str(resolution["tax_domain_hint"])
    gate_status = str(resolution["gate_status"])
    supported_lane_id = cast(str | None, resolution.get("supported_lane_id"))
    historical_version_id = cast(str | None, resolution.get("historical_version_id"))
    regime_identifier = cast(str | None, resolution.get("regime_identifier"))
    followup_resolution = cast(FollowupResolutionResult | None, resolution.get("followup_resolution"))
    turn_resolution_payload = cast(Mapping[str, object] | None, resolution.get("turn_resolution"))
    effective_prompt_text = _optional_string((turn_resolution_payload or {}).get("contextualized_prompt")) or payload.prompt.text
    knowledge_route_payload = cast(
        dict[str, object] | None,
        resolution.get("knowledge_route_payload"),
    )
    execution_knowledge_route_payload = _resolve_execution_knowledge_route_payload(
        prompt_text=effective_prompt_text,
        tax_domain_hint=expected_tax_domain_hint,
        intent_class=expected_intent_class,
        route_payload=knowledge_route_payload,
    )
    _raise_for_existing_execution_idempotency_conflict(
        request=request,
        payload=payload,
        prompt_checksum=prompt_checksum,
        route_payload=execution_knowledge_route_payload,
    )
    plan_payload = cast(dict[str, object], resolution["plan"])
    execution_plan_payload = _resolve_execution_capable_plan(
        plan_payload=plan_payload,
        intent_class=expected_intent_class,
        tax_domain_hint=expected_tax_domain_hint,
    )
    execution_plan_validation = validate_governed_orchestration_plan(
        plan=execution_plan_payload,
        intent_class=expected_intent_class,
        tax_domain_hint=expected_tax_domain_hint,
        for_execution=True,
    )
    is_multi_step_execution = bool(
        execution_plan_payload["planning_mode"] == "multi_step"
        and execution_plan_payload["execution_ready"]
    )
    if (
        payload.intent_class != expected_intent_class
        or payload.tax_domain_hint != expected_tax_domain_hint
    ):
        raise _http_error(
            request=request,
            status_code=400,
            error_code=INVALID_ORCHESTRATION_REQUEST,
            message="Execution context does not match deterministic prompt classification.",
            reason="prompt_context_mismatch",
            reason_code="prompt_context_mismatch",
            context={
                "provided_intent_class": payload.intent_class,
                "expected_intent_class": expected_intent_class,
                "provided_tax_domain_hint": payload.tax_domain_hint,
                "expected_tax_domain_hint": expected_tax_domain_hint,
                "decision_id": payload.decision_id,
            },
        )
    if gate_status == "clarification_required":
        clarification = cast(dict[str, object] | None, resolution.get("clarification")) or {}
        raise _http_error(
            request=request,
            status_code=409,
            error_code="clarification_required",
            message=str(
                clarification.get(
                    "message",
                    "Prompt requires clarification before orchestration execution can continue.",
                )
            ),
            reason=str(clarification.get("reason_code", "clarification_required")),
            reason_code=str(clarification.get("reason_code", "clarification_required")),
            context={
                "required_context_fields": clarification.get("required_context_fields", []),
                "candidate_service_families": clarification.get("candidate_service_families", []),
            },
        )
    if execution_plan_validation["validation_status"] != "accepted":
        error = cast(dict[str, object], execution_plan_validation["error"] or {})
        raise _http_error(
            request=request,
            status_code=409,
            error_code=UNSUPPORTED_PROMPT_SCOPE,
            message="Selected prompt plan is not execution-capable in this orchestration phase.",
            reason=str(error.get("reason", "plan_execution_not_supported")),
            reason_code=str(error.get("reason_code", "plan_execution_not_supported")),
            context={
                "decision_id": payload.decision_id,
                "gate_status": gate_status,
                "plan_id": execution_plan_payload.get("plan_id"),
                "planning_mode": execution_plan_payload.get("planning_mode"),
            },
        )
    expected_selected_route = cast(
        OrchestrationRouteSelection | None, resolution.get("selected_route")
    )
    if not is_multi_step_execution and expected_selected_route is None:
        raise _http_error(
            request=request,
            status_code=409,
            error_code=UNSUPPORTED_PROMPT_SCOPE,
            message="Execution-capable decision did not resolve a concrete route.",
            reason="plan_execution_not_supported",
            reason_code="plan_execution_not_supported",
            context={
                "decision_id": payload.decision_id,
                "plan_id": plan_payload.get("plan_id"),
            },
        )
    if is_multi_step_execution:
        if payload.selected_route is not None:
            raise _http_error(
                request=request,
                status_code=403,
                error_code="unsafe_action_path",
                message="Unsafe action path is blocked by deterministic route guardrail.",
                reason="unsafe_route_override",
                reason_code="unsafe_route_override",
                context={
                    "decision_id": payload.decision_id,
                    "expected_route": None,
                    "provided_route": payload.selected_route.model_dump(mode="json"),
                },
            )
    elif payload.selected_route != expected_selected_route:
        assert expected_selected_route is not None
        assert payload.selected_route is not None
        raise _http_error(
            request=request,
            status_code=403,
            error_code="unsafe_action_path",
            message="Unsafe action path is blocked by deterministic route guardrail.",
            reason="unsafe_route_override",
            reason_code="unsafe_route_override",
            context={
                "decision_id": payload.decision_id,
                "expected_route": expected_selected_route.model_dump(mode="json"),
                "provided_route": payload.selected_route.model_dump(mode="json"),
            },
        )

    if not is_multi_step_execution and payload.selected_route is not None:
        expected_route = _resolve_allowed_execution_route(
            tax_domain_hint=expected_tax_domain_hint,
            selected_route=payload.selected_route,
        )
    else:
        expected_route = None

    if not is_multi_step_execution and expected_route is None:
        assert payload.selected_route is not None
        if (
            SUPPORTED_ROUTE_ACTIONS.get(
                (
                    payload.selected_route.target_service,
                    payload.selected_route.target_operation,
                )
            )
            is None
        ):
            raise _http_error(
                request=request,
                status_code=404,
                error_code=UNSUPPORTED_ORCHESTRATION_ROUTE,
                message="Selected route is not supported for deterministic execution dispatch.",
                reason="unsupported_route_target",
                reason_code="unsupported_route_target",
                context={
                    "route_id": payload.selected_route.route_id,
                    "target_service": payload.selected_route.target_service,
                    "target_operation": payload.selected_route.target_operation,
                    "tax_domain_hint": expected_tax_domain_hint,
                },
            )
        allowed_routes = [
            route.model_dump(mode="json")
            for route in ALLOWED_EXECUTION_ROUTES_BY_DOMAIN.get(
                expected_tax_domain_hint, {}
            ).values()
        ]
        raise _http_error(
            request=request,
            status_code=403,
            error_code="unsafe_action_path",
            message="Unsafe action path is blocked by deterministic route guardrail.",
            reason="unsafe_route_override",
            reason_code="unsafe_route_override",
            context={
                "decision_id": payload.decision_id,
                "expected_route": cast(
                    OrchestrationRouteSelection,
                    resolution["selected_route"],
                ).model_dump(mode="json"),
                "provided_route": payload.selected_route.model_dump(mode="json"),
                "allowed_routes": allowed_routes,
            },
        )
    expected_decision_id = _compute_prompt_decision_id(
        prompt_checksum=str(resolution["prompt_checksum"]),
        intent_class=expected_intent_class,
        plan_id=str(plan_payload["plan_id"]),
        tenant_id=payload.tenant_id,
        conversation_id=payload.conversation_id,
    )
    if payload.decision_id != expected_decision_id:
        raise _http_error(
            request=request,
            status_code=400,
            error_code=INVALID_ROUTE_SELECTION,
            message="Provided decision context does not match deterministic route selection.",
            reason="route_selection_mismatch",
            reason_code="route_selection_mismatch",
            context={
                "decision_id": payload.decision_id,
                "expected_decision_id": expected_decision_id,
                "route_id": (
                    payload.selected_route.route_id if payload.selected_route is not None else None
                ),
            },
        )
    if _is_high_risk_action_context_unsafe(payload.action_context):
        risk_class = payload.action_context.risk_class if payload.action_context else "low"
        confirmation_state = (
            payload.action_context.confirmation_state if payload.action_context else "unknown"
        )
        step_up_proof_state = (
            payload.action_context.step_up_proof_state if payload.action_context else "not_required"
        )
        raise _http_error(
            request=request,
            status_code=403,
            error_code="unsafe_action_path",
            message="High-risk action path requires confirmation and bound step-up proof.",
            reason="unsafe_action_prerequisites_missing",
            reason_code="unsafe_action_prerequisites_missing",
            context={
                "decision_id": payload.decision_id,
                "route_id": (
                    payload.selected_route.route_id if payload.selected_route is not None else None
                ),
                "plan_id": execution_plan_payload["plan_id"],
                "risk_class": risk_class,
                "confirmation_state": confirmation_state,
                "step_up_proof_state": step_up_proof_state,
            },
        )
    if payload.action_context is not None and payload.action_context.risk_class == "high":
        high_risk_safety_decision = evaluate_income_tax_action_safety_controls(
            action_type="submission_execute",
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=cast(int | None, resolution.get("tax_year")),
            correlation_id=get_correlation_id(request),
        )
        if high_risk_safety_decision["control_status"] != "allowed":
            raise _http_error(
                request=request,
                status_code=403,
                error_code="unsafe_action_path",
                message="High-risk action path is blocked by orchestration safety controls.",
                reason=high_risk_safety_decision["reason_code"],
                reason_code=high_risk_safety_decision["reason_code"],
                context={
                    "decision_id": payload.decision_id,
                    "route_id": (
                        payload.selected_route.route_id
                        if payload.selected_route is not None
                        else None
                    ),
                    "plan_id": execution_plan_payload["plan_id"],
                    "risk_class": payload.action_context.risk_class,
                },
            )
    if is_multi_step_execution:
        block_reason = _evaluate_orchestration_feature_gate(
            request=request,
            tenant_id=payload.tenant_id,
            feature_key="compute_plus_grounding_execution",
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=cast(int | None, resolution.get("tax_year")),
        )
        if block_reason is not None:
            raise _http_error(
                request=request,
                status_code=409,
                error_code=UNSUPPORTED_PROMPT_SCOPE,
                message=(
                    "Governed multi-step execution is blocked by orchestration policy controls."
                ),
                reason=block_reason[0],
                reason_code=block_reason[0],
                context={
                    "decision_id": payload.decision_id,
                    "plan_id": execution_plan_payload["plan_id"],
                    "planning_mode": execution_plan_payload["planning_mode"],
                },
            )
    multi_step_errors: list[dict[str, object]] | None = None
    step_results_payload: list[dict[str, object]] | None = None
    step_summary_payload: dict[str, object] | None = None
    adapter_response: dict[str, object] | None = None
    selected_route_payload = (
        payload.selected_route.model_dump(mode="json")
        if payload.selected_route is not None
        else None
    )
    if is_multi_step_execution:
        timed_print("[EXECUTE] About to run governed multi-step plan")
        aggregate = execute_governed_multi_step_plan(
            plan=cast(OrchestrationExecutionPlan, execution_plan_payload),
            intent_class=expected_intent_class,
            tax_domain_hint=expected_tax_domain_hint,
            idempotency_key=payload.idempotency_key,
            correlation_id=get_correlation_id(request),
            trace_id=get_trace_id(request),
            submission_payload_ref=prompt_checksum,
            capability_context={
                "supported_lane_id": supported_lane_id,
                "historical_version_id": historical_version_id,
                "tax_year": cast(int | None, resolution.get("tax_year")),
            },
            auth_context={
                "tenant_id": payload.tenant_id,
                "user_id": payload.effective_taxpayer_user_id,
            },
            knowledge_route_payload=execution_knowledge_route_payload,
            resolve_action_type=resolve_supported_route_action_type,
            dispatch_with_envelope=lambda execution_request: (
                dispatch_route_action_request_with_envelope(
                    execution_request,
                    knowledge_repository=_get_knowledge_repository(request),
                )
            ),
        )
        timed_print(
            "[EXECUTE] Completed governed multi-step plan "
            f"execution_id={aggregate['execution_id']}"
        )
        mapped_result = aggregate["mapped_result"]
        action_status = str(mapped_result.get("action_status", "rejected"))
        if action_status in {"rejected", "retryable_failure"}:
            failed_step = _first_non_resolved_step(step_results=aggregate["step_results"])
            failed_step_service = str(failed_step.get("target_service", "unknown"))
            failed_reason_code = str(mapped_result.get("reason_code", "adapter_execution_rejected"))
            raise _http_error(
                request=request,
                status_code=_status_code_for_route_failure(
                    target_service=failed_step_service,
                    reason_code=failed_reason_code,
                ),
                error_code=(
                    failed_reason_code
                    if failed_step_service == "knowledge"
                    else UNSUPPORTED_ORCHESTRATION_ROUTE
                ),
                message=(
                    "Deterministic knowledge grounding rejected selected orchestration route."
                    if failed_step_service == "knowledge"
                    else (
                        "Deterministic multi-step adapter execution rejected selected "
                        "orchestration route."
                    )
                ),
                reason=failed_reason_code,
                reason_code=failed_reason_code,
                context={
                    "plan_id": aggregate["plan"]["plan_id"],
                    "step_id": failed_step.get("step_id"),
                    "route_id": failed_step.get("route_id"),
                    "target_service": failed_step.get("target_service"),
                    "target_operation": failed_step.get("target_operation"),
                    "action_status": action_status,
                },
            )
        execution_id = str(aggregate["execution_id"])
        plan_payload = cast(dict[str, object], aggregate["plan"])
        grounded_evidence = aggregate.get("grounded_evidence")
        step_results_payload = [cast(dict[str, object], step) for step in aggregate["step_results"]]
        step_summary_payload = cast(dict[str, object], aggregate["step_summary"])
        multi_step_errors = aggregate.get("errors")
        selected_route_payload = None
    else:
        assert payload.selected_route is not None
        if followup_resolution is not None and followup_resolution["reuse_prior_service_result"]:
            synthetic_execution = _build_followup_context_execution(
                request=request,
                payload=payload,
                resolution=resolution,
                followup_resolution=followup_resolution,
                selected_route=payload.selected_route,
                prompt_checksum=prompt_checksum,
            )
            mapped_result = synthetic_execution["mapped_result"]
            action_status = str(mapped_result.get("action_status", "accepted"))
            adapter_response = synthetic_execution["adapter_response"]
            execution_id = str(synthetic_execution["execution_id"])
            plan_payload = synthetic_execution["plan"]
            grounded_evidence = _extract_grounded_evidence(adapter_response)
        elif (
            payload.selected_route.target_service == "orchestration"
            and payload.selected_route.target_operation == "generate_meta_conversation_response"
        ):
            synthetic_execution = _build_meta_conversation_execution(
                request=request,
                payload=payload,
                resolution=resolution,
                followup_resolution=followup_resolution,
                selected_route=payload.selected_route,
                prompt_checksum=prompt_checksum,
            )
            mapped_result = synthetic_execution["mapped_result"]
            action_status = str(mapped_result.get("action_status", "accepted"))
            adapter_response = synthetic_execution["adapter_response"]
            execution_id = str(synthetic_execution["execution_id"])
            plan_payload = synthetic_execution["plan"]
            grounded_evidence = _extract_grounded_evidence(adapter_response)
        else:
            mapped_action_type = resolve_supported_route_action_type(
                target_service=payload.selected_route.target_service,
                target_operation=payload.selected_route.target_operation,
            )
            if mapped_action_type is None:
                raise _http_error(
                    request=request,
                    status_code=404,
                    error_code=UNSUPPORTED_ORCHESTRATION_ROUTE,
                    message="Selected route is not supported for deterministic execution dispatch.",
                    reason="unsupported_route_target",
                    reason_code="unsupported_route_target",
                    context={
                        "route_id": payload.selected_route.route_id,
                        "target_service": payload.selected_route.target_service,
                        "target_operation": payload.selected_route.target_operation,
                    },
                )

            execution_request: ActionExecutionRequest = {
                "idempotency_key": payload.idempotency_key,
                "correlation_id": get_correlation_id(request),
                "action_type": mapped_action_type,
                "submission_payload_ref": prompt_checksum,
                "capability_context": {
                    "supported_lane_id": supported_lane_id,
                    "historical_version_id": historical_version_id,
                    "tax_year": cast(int | None, resolution.get("tax_year")),
                },
                "trace_id": get_trace_id(request),
                "route_id": payload.selected_route.route_id,
                "target_service": payload.selected_route.target_service,
                "target_operation": payload.selected_route.target_operation,
                "auth_context": {
                    "tenant_id": payload.tenant_id,
                    "user_id": payload.effective_taxpayer_user_id,
                },
            }
            if execution_knowledge_route_payload is not None:
                execution_request["route_payload"] = execution_knowledge_route_payload
            emit_orchestration_debug(
                "RUNTIME",
                "execution.adapter.requested",
                route_id=payload.selected_route.route_id,
                target_service=payload.selected_route.target_service,
                target_operation=payload.selected_route.target_operation,
                action_type=mapped_action_type,
                correlation_id=get_correlation_id(request),
                trace_id=get_trace_id(request),
            )
            timed_print("[EXECUTE] About to dispatch route adapter request")
            envelope = dispatch_route_action_request_with_envelope(
                execution_request,
                knowledge_repository=_get_knowledge_repository(request),
            )
            timed_print(
                "[EXECUTE] Dispatched route adapter request "
                f"route_id={payload.selected_route.route_id}"
            )
            adapter_response_candidate = cast(dict[str, object] | None, envelope.get("adapter_response"))
            emit_orchestration_debug(
                "RUNTIME",
                "execution.adapter.completed",
                route_id=payload.selected_route.route_id,
                target_service=payload.selected_route.target_service,
                target_operation=payload.selected_route.target_operation,
                adapter_status=(
                    _optional_string(adapter_response_candidate.get("adapter_status"))
                    if isinstance(adapter_response_candidate, dict)
                    else None
                ),
                action_result_code=(
                    _optional_string(adapter_response_candidate.get("action_result_code"))
                    if isinstance(adapter_response_candidate, dict)
                    else None
                ),
                correlation_id=get_correlation_id(request),
                trace_id=get_trace_id(request),
            )
            if envelope["execution_status"] != "resolved":
                error = cast(dict[str, object], envelope.get("error") or {})
                raise _http_error(
                    request=request,
                    status_code=400,
                    error_code=str(error.get("error_code", INVALID_ORCHESTRATION_REQUEST)),
                    message=str(
                        error.get(
                            "message",
                            "Execution request failed deterministic idempotency checks.",
                        )
                    ),
                    reason=str(error.get("reason", "invalid_execution_context")),
                    reason_code=str(error.get("reason_code", "invalid_execution_context")),
                    context=cast(dict[str, object] | None, error.get("rejected_context")),
                )

            mapped_result = cast(dict[str, object], envelope["mapped_result"])
            action_status = str(mapped_result.get("action_status", "rejected"))
            if action_status in {"rejected", "retryable_failure"}:
                reason_code = str(mapped_result.get("reason_code", "adapter_execution_rejected"))
                emit_orchestration_debug(
                    "RUNTIME",
                    "execution.adapter.rejected",
                    route_id=payload.selected_route.route_id,
                    target_service=payload.selected_route.target_service,
                    target_operation=payload.selected_route.target_operation,
                    action_status=action_status,
                    reason_code=reason_code,
                    correlation_id=get_correlation_id(request),
                    trace_id=get_trace_id(request),
                )
                raise _http_error(
                    request=request,
                    status_code=_status_code_for_route_failure(
                        target_service=payload.selected_route.target_service,
                        reason_code=reason_code,
                    ),
                    error_code=(
                        reason_code
                        if payload.selected_route.target_service == "knowledge"
                        else UNSUPPORTED_ORCHESTRATION_ROUTE
                    ),
                    message=(
                        "Deterministic knowledge grounding rejected selected orchestration route."
                        if payload.selected_route.target_service == "knowledge"
                        else (
                            "Deterministic adapter execution rejected selected orchestration route."
                        )
                    ),
                    reason=reason_code,
                    reason_code=reason_code,
                    context={
                        "route_id": payload.selected_route.route_id,
                        "target_service": payload.selected_route.target_service,
                        "target_operation": payload.selected_route.target_operation,
                        "action_status": action_status,
                    },
                )

            adapter_response = cast(dict[str, object] | None, envelope.get("adapter_response"))
            if adapter_response is None:
                raise _http_error(
                    request=request,
                    status_code=502,
                    error_code=UNSUPPORTED_ORCHESTRATION_ROUTE,
                    message=(
                        "Deterministic adapter execution did not return adapter response payload."
                    ),
                    reason="adapter_response_missing",
                    reason_code="adapter_response_missing",
                )
            execution_trace = cast(dict[str, object], envelope["trace"])
            execution_id = str(execution_trace["execution_envelope_id"])
            plan_payload = cast(dict[str, object], envelope["plan"])
            grounded_evidence = _extract_grounded_evidence(adapter_response)
    grounding_status: str | None = None
    explanation_payload: GroundedExplanationPayload | None = None
    explanation_status: str | None = None
    lineage_refs: dict[str, object] = {
        "decision_id": payload.decision_id,
        "execution_id": execution_id,
        "prompt_checksum": prompt_checksum,
        "route_id": (
            payload.selected_route.route_id if payload.selected_route is not None else None
        ),
        "target_service": (
            payload.selected_route.target_service if payload.selected_route is not None else None
        ),
        "target_operation": (
            payload.selected_route.target_operation if payload.selected_route is not None else None
        ),
        "idempotency_key": payload.idempotency_key,
        "tenant_id": payload.tenant_id,
        "user_id": payload.effective_taxpayer_user_id,
        "tax_domain_hint": expected_tax_domain_hint,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "regime_identifier": regime_identifier,
        "planning_mode": plan_payload["planning_mode"],
    }
    if step_summary_payload is not None:
        lineage_refs["step_summary"] = step_summary_payload
    if grounded_evidence is not None:
        grounding_status = "grounded"
        lineage_refs["grounding_status"] = grounding_status
        lineage_refs["grounded_evidence"] = grounded_evidence
        if grounded_evidence:
            first_grounding = grounded_evidence[0]
            lineage_refs["source_id"] = first_grounding["source_id"]
            lineage_refs["source_version_id"] = first_grounding["source_version_id"]
            lineage_refs["anchor_id"] = first_grounding["anchor_id"]
            lineage_refs["authority_level"] = first_grounding["authority_level"]
            lineage_refs["effective_from"] = first_grounding["effective_from"]
            lineage_refs["effective_to"] = first_grounding["effective_to"]
            lineage_refs["tax_year"] = first_grounding.get("tax_year", datetime.now().year)
    else:
        grounding_status = "not_applicable"
    if supports_grounded_explanation_intent(expected_intent_class):
        if grounded_evidence is None:
            raise _http_error(
                request=request,
                status_code=409,
                error_code=UNSUPPORTED_PROMPT_SCOPE,
                message="Grounded explanation requires governed evidence and none was available.",
                reason="insufficient_grounded_evidence",
                reason_code="insufficient_grounded_evidence",
                context={
                    "route_id": (
                        payload.selected_route.route_id
                        if payload.selected_route is not None
                        else None
                    ),
                    "target_service": (
                        payload.selected_route.target_service
                        if payload.selected_route is not None
                        else None
                    ),
                    "target_operation": (
                        payload.selected_route.target_operation
                        if payload.selected_route is not None
                        else None
                    ),
                },
            )
        try:
            timed_print("[GROUNDING] About to render grounded explanation")
            explanation_payload = render_grounded_explanation(
                grounded_evidence=grounded_evidence,
            )
            timed_print(
                "[GROUNDING] Rendered grounded explanation "
                f"citation_count={len(explanation_payload['citations'])}"
            )
            explanation_status = str(explanation_payload["explanation_status"])
            lineage_refs["explanation_status"] = explanation_status
            lineage_refs["citations"] = explanation_payload["citations"]
            lineage_refs["authority_summary"] = explanation_payload["authority_summary"]
            lineage_refs["temporal_applicability"] = explanation_payload["temporal_applicability"]
        except GroundedExplanationError as error:
            raise _http_error(
                request=request,
                status_code=409,
                error_code=error.error_code,
                message=error.message,
                reason=error.reason,
                reason_code=error.reason,
                context={
                    "route_id": (
                        payload.selected_route.route_id
                        if payload.selected_route is not None
                        else None
                    ),
                    "target_service": (
                        payload.selected_route.target_service
                        if payload.selected_route is not None
                        else None
                    ),
                    "target_operation": (
                        payload.selected_route.target_operation
                        if payload.selected_route is not None
                        else None
                    ),
                },
            ) from error
    else:
        explanation_status = "not_applicable"
    explanation_items_payload = (
        explanation_payload["explanation_items"] if explanation_payload is not None else None
    )
    citations_payload = (
        explanation_payload["citations"] if explanation_payload is not None else None
    )
    authority_summary_payload = (
        explanation_payload["authority_summary"] if explanation_payload is not None else None
    )
    temporal_applicability_payload = (
        explanation_payload["temporal_applicability"] if explanation_payload is not None else None
    )
    governed_validation = _build_orchestration_governed_validation(
        selected_route=payload.selected_route,
        tax_domain_hint=expected_tax_domain_hint,
        adapter_response=adapter_response,
    )
    if governed_validation is not None and adapter_response is not None:
        adapter_response["validation"] = governed_validation
    emit_income_tax_audit_event(
        event_type="prompt_execution_resolved",
        status="executed",
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=cast(int | None, resolution.get("tax_year")),
        context={
            "tenant_id": payload.tenant_id,
            "user_id": payload.effective_taxpayer_user_id,
            "authorization_outcome": "allowed",
            "authorization_role": getattr(request.state, "authorization_role", None),
            "effective_taxpayer_owner_ref": getattr(
                request.state, "effective_taxpayer_user_id", None
            ),
            "delegation_id": getattr(request.state, "authorization_delegation_id", None),
            "resource_id": execution_id,
            "decision_id": payload.decision_id,
            "prompt_checksum": prompt_checksum,
            "route_id": (
                payload.selected_route.route_id if payload.selected_route is not None else None
            ),
            "target_service": (
                payload.selected_route.target_service
                if payload.selected_route is not None
                else None
            ),
            "target_operation": (
                payload.selected_route.target_operation
                if payload.selected_route is not None
                else None
            ),
            "plan_id": plan_payload["plan_id"],
            "planning_mode": plan_payload["planning_mode"],
        },
    )
    base_final_outcome = build_income_tax_final_outcome_envelope(
        outcome_status=map_action_status_to_outcome_status(action_status),
        message="Orchestration execution completed with deterministic final outcome envelope.",
        result={
            "decision_id": payload.decision_id,
            "prompt_checksum": prompt_checksum,
            "plan": plan_payload,
            "selected_route": selected_route_payload,
            "step_results": step_results_payload,
            "step_summary": step_summary_payload,
            "execution_status": "resolved",
            "mapped_result": mapped_result,
            "adapter_response": adapter_response,
            "validation": governed_validation,
            "grounding_status": grounding_status,
            "grounded_evidence": grounded_evidence,
            "explanation_status": explanation_status,
            "explanation_items": explanation_items_payload,
            "citations": citations_payload,
            "authority_summary": authority_summary_payload,
            "temporal_applicability": temporal_applicability_payload,
        },
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        lineage_refs=lineage_refs,
        audit_events=list_income_tax_audit_events(correlation_id=get_correlation_id(request)),
    )
    synthesis_errors = list(multi_step_errors or [])
    current_stated_facts = cast(
        ExtractedTaxpayerFacts,
        resolution.get("stated_facts", {}),
    )
    prior_stated_facts_record = cast(
        ConversationStateRecord | None,
        resolution.get("prior_stated_facts_record"),
    )
    prior_stated_facts: ExtractedTaxpayerFacts | None = None
    prior_execution_id: str | None = None
    if prior_stated_facts_record is not None:
        prior_stated_facts = unprotect_stated_facts(
            protected_stated_facts=prior_stated_facts_record["context_payload"].get("stated_facts"),
            protector=_get_conversation_state_protector(request),
        )
        if prior_stated_facts is not None:
            prior_execution_id = prior_stated_facts_record["execution_id"]
    fact_mismatches = (
        compare_stated_facts(
            current=current_stated_facts,
            prior=prior_stated_facts,
            prior_execution_id=prior_execution_id,
        )
        if prior_stated_facts is not None and prior_execution_id is not None
        else []
    )
    fact_mismatches_detected = _audit_fact_mismatch_summary(fact_mismatches)
    synthesized_response: UnifiedAnswerResponseModel
    synthesis_context: GovernedSynthesisContext | None = None
    try:
        emit_orchestration_debug(
            "RUNTIME",
            "synthesis.context.requested",
            decision_id=payload.decision_id,
            prompt_checksum=prompt_checksum,
            correlation_id=get_correlation_id(request),
            trace_id=get_trace_id(request),
        )
        timed_print("[SYNTHESIS_CONTEXT] About to build governed synthesis context")
        synthesis_context = build_governed_synthesis_context(
            prompt_text=effective_prompt_text,
            tax_domain_hint=expected_tax_domain_hint,
            intent_class=expected_intent_class,
            plan=plan_payload,
            mapped_result=mapped_result,
            final_outcome=cast(dict[str, object], base_final_outcome),
            selected_route=selected_route_payload,
            adapter_response=adapter_response,
            step_results=step_results_payload,
            step_summary=step_summary_payload,
            grounded_evidence=grounded_evidence,
            explanation_items=explanation_items_payload,
            citations=citations_payload,
            authority_summary=authority_summary_payload,
            temporal_applicability=temporal_applicability_payload,
            conversation_context_summary=(
                followup_resolution["conversation_context_summary"]
                if followup_resolution is not None
                else None
            ),
            prior_stated_facts=prior_stated_facts,
            prior_execution_id=prior_execution_id,
            fact_mismatches=fact_mismatches,
        )
        synthesis_context["synthesis_tool_runtime"] = {
            "correlation_id": get_correlation_id(request),
            "trace_id": get_trace_id(request),
            "execution_id": execution_id,
            "tenant_id": payload.tenant_id,
            "user_id": payload.effective_taxpayer_user_id,
            "supported_lane_id": supported_lane_id,
            "historical_version_id": historical_version_id,
            "tax_year": cast(int | None, resolution.get("tax_year")),
        }
        timed_print(
            "[SYNTHESIS_CONTEXT] Built governed synthesis context "
            f"answer_mode={synthesis_context['answer_mode']!r} "
            f"citation_count={len(synthesis_context['citations'])} "
            f"grounded_evidence_count={len(grounded_evidence or [])}"
        )
        emit_orchestration_debug(
            "RUNTIME",
            "synthesis.context.built",
            decision_id=payload.decision_id,
            answer_mode=synthesis_context["answer_mode"],
            grounded_evidence_count=len(grounded_evidence or []),
            citation_count=len(synthesis_context["citations"]),
            correlation_id=get_correlation_id(request),
            trace_id=get_trace_id(request),
        )
        timed_print("We have built governed synthesis context...")
        synthesis_block_reason = _evaluate_orchestration_feature_gate(
            request=request,
            tenant_id=payload.tenant_id,
            feature_key="response_synthesis",
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=cast(int | None, resolution.get("tax_year")),
        )
        timed_print("WE have evaluated orchestration feature gate...")
        if synthesis_block_reason is None and requires_grounded_legal_basis_synthesis(
            synthesis_context["answer_mode"]
        ):
            timed_print("There is not a synthesis block reason")
            synthesis_block_reason = _evaluate_orchestration_feature_gate(
                request=request,
                tenant_id=payload.tenant_id,
                feature_key="grounded_legal_basis_synthesis",
                supported_lane_id=supported_lane_id,
                historical_version_id=historical_version_id,
                tax_year=cast(int | None, resolution.get("tax_year")),
            )
        if synthesis_block_reason is not None:
            timed_print("There is a synthesis block...")
            emit_orchestration_debug(
                "RUNTIME",
                "synthesis.generation.failed",
                decision_id=payload.decision_id,
                answer_mode=synthesis_context["answer_mode"],
                reason_code=synthesis_block_reason[0],
                correlation_id=get_correlation_id(request),
                trace_id=get_trace_id(request),
            )
            emit_income_tax_audit_event(
                event_type="response_synthesis_failed",
                status="failed",
                correlation_id=get_correlation_id(request),
                trace_id=get_trace_id(request),
                supported_lane_id=supported_lane_id,
                historical_version_id=historical_version_id,
                tax_year=cast(int | None, resolution.get("tax_year")),
                context={
                    "tenant_id": payload.tenant_id,
                    "user_id": payload.effective_taxpayer_user_id,
                    "resource_id": execution_id,
                    "decision_id": payload.decision_id,
                    "reason_code": synthesis_block_reason[0],
                    "unsupported_claims": [],
                    "contradictions_found": [],
                    "grounding_contradictions": list(synthesis_context["grounding_contradictions"]),
                    "fact_mismatches_detected": fact_mismatches_detected,
                    "verification_retry_used": False,
                },
            )
            synthesis_errors.append(
                _build_nonfatal_error(
                    request=request,
                    error_code="response_synthesis_unavailable",
                    message=synthesis_block_reason[1],
                    reason_code=synthesis_block_reason[0],
                    context={
                        "answer_mode": synthesis_context["answer_mode"],
                        "plan_id": plan_payload["plan_id"],
                    },
                )
            )
            timed_print(f"synthesis_errors={synthesis_errors}")
            synthesized_response = build_failed_unified_answer_response(
                answer_mode=synthesis_context["answer_mode"],
                citations=[
                    UnifiedAnswerCitationModel.model_validate(item)
                    for item in synthesis_context["citations"]
                ],
                assumptions=[
                    str(item) for item in cast(list[object], synthesis_context["assumptions"])
                ],
                warnings=[
                    *[str(item) for item in cast(list[object], synthesis_context["warnings"])],
                    synthesis_block_reason[1],
                ],
            )
            synthesized_response.integrity_signals.grounding_contradictions = list(
                synthesis_context["grounding_contradictions"]
            )
            integrity_signals = synthesized_response.integrity_signals
            integrity_signals.unverified_or_contradicting_user_facts = (
                _merge_user_fact_integrity_signals(
                    fact_mismatches=fact_mismatches,
                    model_signals=(integrity_signals.unverified_or_contradicting_user_facts),
                )
            )
        else:
            timed_print("Synthesis has been requested...")
            emit_orchestration_debug(
                "RUNTIME",
                "synthesis.generate.requested",
                decision_id=payload.decision_id,
                answer_mode=synthesis_context["answer_mode"],
                citation_count=len(synthesis_context["citations"]),
                correlation_id=get_correlation_id(request),
                trace_id=get_trace_id(request),
            )
            emit_income_tax_audit_event(
                event_type="response_synthesis_requested",
                status="requested",
                correlation_id=get_correlation_id(request),
                trace_id=get_trace_id(request),
                supported_lane_id=supported_lane_id,
                historical_version_id=historical_version_id,
                tax_year=cast(int | None, resolution.get("tax_year")),
                context={
                    "tenant_id": payload.tenant_id,
                    "user_id": payload.effective_taxpayer_user_id,
                    "resource_id": execution_id,
                    "decision_id": payload.decision_id,
                    "answer_mode": synthesis_context["answer_mode"],
                    "grounding_contradictions": list(synthesis_context["grounding_contradictions"]),
                    "fact_mismatches_detected": fact_mismatches_detected,
                    "verification_retry_used": False,
                },
            )
            timed_print("[SYNTHESIS] About to generate synthesized response")
            synthesized_response = _get_llm_response_generator(request).generate(
                synthesis_context
            )
            timed_print(
                "[SYNTHESIS] Generated synthesized response "
                f"citation_count={len(synthesized_response.citations)}"
            )
            emit_orchestration_debug(
                "RUNTIME",
                "synthesis.generate.completed",
                decision_id=payload.decision_id,
                answer_mode=synthesis_context["answer_mode"],
                citation_count=len(synthesized_response.citations),
                correlation_id=get_correlation_id(request),
                trace_id=get_trace_id(request),
            )
            emit_orchestration_debug(
                "RUNTIME",
                "verification.started",
                decision_id=payload.decision_id,
                answer_mode=synthesis_context["answer_mode"],
                correlation_id=get_correlation_id(request),
                trace_id=get_trace_id(request),
            )
            timed_print("[VERIFICATION] About to verify synthesized response")
            verification_result = _verify_synthesized_answer(
                answer=synthesized_response,
                grounded_evidence=grounded_evidence or [],
                original_prompt=effective_prompt_text,
                resolved_tax_domain=(
                    expected_tax_domain_hint
                    if requires_grounded_legal_basis_synthesis(synthesis_context["answer_mode"])
                    else None
                ),
                resolved_entity=(
                    _optional_string((execution_knowledge_route_payload or {}).get("resolved_entity"))
                    if requires_grounded_legal_basis_synthesis(synthesis_context["answer_mode"])
                    else None
                ),
            )
            timed_print(
                "[VERIFICATION] Verified synthesized response "
                f"is_verified={verification_result.get('is_verified')} "
                f"failed_checks={len(cast(list[object], verification_result.get('failed_checks', [])))}"
            )
            retry_count = 0
            verification_retry_used = False
            if (
                not bool(verification_result["is_verified"])
                and retry_count < MAX_VERIFICATION_RETRIES
            ):
                emit_orchestration_debug(
                    "RUNTIME",
                    "verification.retry.requested",
                    decision_id=payload.decision_id,
                    failed_checks=cast(list[object], verification_result.get("failed_checks", [])),
                    correlation_id=get_correlation_id(request),
                    trace_id=get_trace_id(request),
                )
                retry_count += 1
                verification_retry_used = True
                retry_context = inject_verification_failure_reasons(
                    synthesis_context,
                    verification_result,
                )
                timed_print("[VERIFICATION] About to regenerate synthesized response for retry")
                synthesized_response = _get_llm_response_generator(request).generate(
                    retry_context
                )
                timed_print(
                    "[VERIFICATION] Regenerated synthesized response for retry "
                    f"citation_count={len(synthesized_response.citations)}"
                )
                emit_orchestration_debug(
                    "RUNTIME",
                    "verification.retry.completed",
                    decision_id=payload.decision_id,
                    answer_mode=synthesis_context["answer_mode"],
                    correlation_id=get_correlation_id(request),
                    trace_id=get_trace_id(request),
                )
                verification_result = _verify_synthesized_answer(
                    answer=synthesized_response,
                    grounded_evidence=grounded_evidence or [],
                    original_prompt=effective_prompt_text,
                    resolved_tax_domain=(
                        expected_tax_domain_hint
                        if requires_grounded_legal_basis_synthesis(synthesis_context["answer_mode"])
                        else None
                    ),
                    resolved_entity=(
                        _optional_string(
                            (execution_knowledge_route_payload or {}).get("resolved_entity")
                        )
                        if requires_grounded_legal_basis_synthesis(synthesis_context["answer_mode"])
                        else None
                    ),
                )
                timed_print(
                    "[VERIFICATION] Verified synthesized response after retry "
                    f"is_verified={verification_result.get('is_verified')}"
                )
            if (
                requires_grounded_legal_basis_synthesis(synthesis_context["answer_mode"])
                and "domain_subject_alignment"
                in cast(list[str], verification_result["failed_checks"])
            ):
                # Do not return a polished, cited answer when the citation is
                # only a member of the retrieved set but not about the user's
                # resolved subject.  This is a user-facing clarification, not
                # merely an integrity flag hidden in telemetry.
                synthesized_response = build_failed_unified_answer_response(
                    answer_mode=synthesis_context["answer_mode"],
                    citations=[],
                    assumptions=[],
                    warnings=[
                        "I could not verify that the retrieved passage matches the tax "
                        "you meant. Please name the tax or subject so I can check it safely."
                    ],
                )
            synthesized_response.integrity_signals.verification_is_verified = bool(
                verification_result["is_verified"]
            )
            confidence_score = verification_result.get("confidence_score")
            synthesized_response.integrity_signals.verification_confidence = (
                float(confidence_score) if isinstance(confidence_score, int | float) else 0.0
            )
            synthesized_response.integrity_signals.grounding_contradictions = list(
                synthesis_context["grounding_contradictions"]
            )
            integrity_signals = synthesized_response.integrity_signals
            integrity_signals.unverified_or_contradicting_user_facts = (
                _merge_user_fact_integrity_signals(
                    fact_mismatches=fact_mismatches,
                    model_signals=(integrity_signals.unverified_or_contradicting_user_facts),
                )
            )
            timed_print(f"verification_result={verification_result}")
            emit_orchestration_debug(
                "RUNTIME",
                "verification.completed",
                decision_id=payload.decision_id,
                answer_mode=synthesis_context["answer_mode"],
                is_verified=verification_result.get("is_verified"),
                confidence_score=verification_result.get("confidence_score"),
                failed_checks=verification_result.get("failed_checks"),
                correlation_id=get_correlation_id(request),
                trace_id=get_trace_id(request),
            )
            emit_income_tax_audit_event(
                event_type="response_synthesis_resolved",
                status="generated",
                correlation_id=get_correlation_id(request),
                trace_id=get_trace_id(request),
                supported_lane_id=supported_lane_id,
                historical_version_id=historical_version_id,
                tax_year=cast(int | None, resolution.get("tax_year")),
                context={
                    "tenant_id": payload.tenant_id,
                    "user_id": payload.effective_taxpayer_user_id,
                    "resource_id": execution_id,
                    "decision_id": payload.decision_id,
                    "answer_mode": synthesis_context["answer_mode"],
                    "verification_confidence": verification_result["confidence_score"],
                    "verification_is_verified": verification_result["is_verified"],
                    "verification_retry_used": verification_retry_used,
                    "unsupported_claims": list(
                        synthesized_response.integrity_signals.unsupported_claims
                    ),
                    "contradictions_found": list(
                        synthesized_response.integrity_signals.contradictions_found
                    ),
                    "grounding_contradictions": list(
                        synthesized_response.integrity_signals.grounding_contradictions
                    ),
                    "fact_mismatches_detected": fact_mismatches_detected,
                },
            )
    except SynthesisContextError as error:
        timed_print("Error occured")
        emit_income_tax_audit_event(
            event_type="response_synthesis_failed",
            status="failed",
            correlation_id=get_correlation_id(request),
            trace_id=get_trace_id(request),
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=cast(int | None, resolution.get("tax_year")),
            context={
                "tenant_id": payload.tenant_id,
                "user_id": payload.effective_taxpayer_user_id,
                "resource_id": execution_id,
                "decision_id": payload.decision_id,
                "reason_code": error.reason_code,
                "unsupported_claims": [],
                "contradictions_found": [],
                "grounding_contradictions": (
                    list(synthesis_context["grounding_contradictions"])
                    if synthesis_context is not None
                    else []
                ),
                "fact_mismatches_detected": fact_mismatches_detected,
                "verification_retry_used": False,
            },
        )
        synthesis_errors.append(
            _build_nonfatal_error(
                request=request,
                error_code=error.error_code,
                message=error.message,
                reason_code=error.reason_code,
                context=error.context,
            )
        )
        synthesized_response = build_failed_unified_answer_response(
            answer_mode="unsupported",
        )
    except LLMResponseGenerationError as error:
        timed_print("LLm response generation error")
        emit_income_tax_audit_event(
            event_type="response_synthesis_failed",
            status="failed",
            correlation_id=get_correlation_id(request),
            trace_id=get_trace_id(request),
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=cast(int | None, resolution.get("tax_year")),
            context={
                "tenant_id": payload.tenant_id,
                "user_id": payload.effective_taxpayer_user_id,
                "resource_id": execution_id,
                "decision_id": payload.decision_id,
                "reason_code": error.reason_code,
                "unsupported_claims": [],
                "contradictions_found": [],
                "grounding_contradictions": (
                    list(synthesis_context["grounding_contradictions"])
                    if synthesis_context is not None
                    else []
                ),
                "fact_mismatches_detected": fact_mismatches_detected,
                "verification_retry_used": False,
            },
        )
        synthesis_errors.append(
            _build_nonfatal_error(
                request=request,
                error_code=error.error_code,
                message=error.message,
                reason_code=error.reason_code,
                context=error.context,
            )
        )
        timed_print(f"synthesis_errors={synthesis_errors}")
        synthesized_response = build_failed_unified_answer_response(
            answer_mode=(
                synthesis_context["answer_mode"]
                if synthesis_context is not None
                else _fallback_answer_mode(
                    expected_intent_class=expected_intent_class,
                    selected_route=payload.selected_route,
                )
            ),
            citations=(
                [
                    UnifiedAnswerCitationModel.model_validate(item)
                    for item in synthesis_context["citations"]
                ]
                if synthesis_context is not None
                else []
            ),
            assumptions=(
                [str(item) for item in cast(list[object], synthesis_context["assumptions"])]
                if synthesis_context is not None
                else []
            ),
            warnings=(
                [str(item) for item in cast(list[object], synthesis_context["warnings"])]
                if synthesis_context is not None
                else []
            ),
        )
        if synthesis_context is not None:
            synthesized_response.integrity_signals.grounding_contradictions = list(
                synthesis_context["grounding_contradictions"]
            )
        synthesized_response.integrity_signals.unverified_or_contradicting_user_facts = list(
            fact_mismatches
        )
    integrity_signals = synthesized_response.integrity_signals
    integrity_signals.unverified_or_contradicting_user_facts = _merge_user_fact_integrity_signals(
        fact_mismatches=fact_mismatches,
        model_signals=integrity_signals.unverified_or_contradicting_user_facts,
    )
    missing_fact_fields = _post_grounding_missing_fact_fields(
        integrity_signals.unverified_or_contradicting_user_facts
    )
    if (
        missing_fact_fields
        and synthesized_response.answer_mode != "grounded_knowledge"
    ):
        _raise_for_post_grounding_fact_clarification(
            request=request,
            missing_fact_fields=missing_fact_fields,
        )
    integrity_signals.confidence_flag = _compute_confidence_flag(integrity_signals)
    try:
        timed_print("persistent conversation state..")
        _persist_conversation_state(
            request=request,
            payload=payload,
            execution_id=execution_id,
            prompt_checksum=prompt_checksum,
            effective_prompt_text=effective_prompt_text,
            raw_prompt_text=payload.prompt.text,
            assistant_answer_text=synthesized_response.answer_text,
            assistant_answer_summary=bounded_preview(synthesized_response.answer_text),
            turn_resolution=turn_resolution_payload,
            intent_class=expected_intent_class,
            tax_domain_hint=expected_tax_domain_hint,
            selected_route=selected_route_payload,
            plan=plan_payload,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            regime_identifier=regime_identifier,
            tax_year=cast(int | None, resolution.get("tax_year")),
            mapped_result=mapped_result,
            adapter_response=adapter_response,
            grounded_evidence=grounded_evidence,
            citations=citations_payload,
            stated_facts=cast(ExtractedTaxpayerFacts, resolution.get("stated_facts", {})),
        )
    except ConversationStateStoreError as error:
        synthesis_errors.append(
            _build_nonfatal_error(
                request=request,
                error_code="conversation_context_persistence_unavailable",
                message=error.message,
                reason_code=error.reason_code,
                context={"execution_id": execution_id},
            )
        )
    final_outcome = build_income_tax_final_outcome_envelope(
        outcome_status=map_action_status_to_outcome_status(action_status),
        message="Orchestration execution completed with deterministic final outcome envelope.",
        result={
            "decision_id": payload.decision_id,
            "prompt_checksum": prompt_checksum,
            "plan": plan_payload,
            "selected_route": selected_route_payload,
            "step_results": step_results_payload,
            "step_summary": step_summary_payload,
            "execution_status": "resolved",
            "mapped_result": mapped_result,
            "adapter_response": adapter_response,
            "validation": governed_validation,
            "grounding_status": grounding_status,
            "grounded_evidence": grounded_evidence,
            "explanation_status": explanation_status,
            "explanation_items": explanation_items_payload,
            "citations": citations_payload,
            "authority_summary": authority_summary_payload,
            "temporal_applicability": temporal_applicability_payload,
            "response": synthesized_response.model_dump(mode="json"),
        },
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        lineage_refs=lineage_refs,
        audit_events=list_income_tax_audit_events(correlation_id=get_correlation_id(request)),
    )
    return PromptExecutionResponse(
        status="executed",
        service=SERVICE_NAME,
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        execution_id=execution_id,
        decision_id=payload.decision_id,
        prompt_checksum=prompt_checksum,
        tax_domain_hint=expected_tax_domain_hint,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        regime_identifier=regime_identifier,
        plan=OrchestrationPlanModel.model_validate(plan_payload),
        grounding_status=cast(Literal["grounded", "not_applicable"] | None, grounding_status),
        grounded_evidence=(
            [GroundedKnowledgeEvidence.model_validate(item) for item in grounded_evidence]
            if grounded_evidence is not None
            else None
        ),
        explanation_status=cast(
            Literal["grounded", "not_applicable"] | None,
            explanation_status,
        ),
        explanation_items=(
            [GroundedExplanationItem.model_validate(item) for item in explanation_items_payload]
            if explanation_items_payload is not None
            else None
        ),
        citations=(
            [GroundedExplanationCitation.model_validate(item) for item in citations_payload]
            if citations_payload is not None
            else None
        ),
        source_references=list(synthesized_response.source_references),
        authority_summary=(
            GroundedAuthoritySummary.model_validate(authority_summary_payload)
            if authority_summary_payload is not None
            else None
        ),
        temporal_applicability=(
            GroundedTemporalApplicability.model_validate(temporal_applicability_payload)
            if temporal_applicability_payload is not None
            else None
        ),
        selected_route=payload.selected_route,
        step_results=(
            [
                OrchestrationStepExecutionResultModel.model_validate(item)
                for item in step_results_payload
            ]
            if step_results_payload is not None
            else None
        ),
        step_summary=(
            OrchestrationStepExecutionSummaryModel.model_validate(step_summary_payload)
            if step_summary_payload is not None
            else None
        ),
        execution_status="resolved",
        mapped_result=mapped_result,
        adapter_response=adapter_response,
        validation=governed_validation,
        response=synthesized_response,
        final_outcome=cast(dict[str, object], final_outcome),
        errors=synthesis_errors or None,
    )


@ROUTER.get("/v1/orchestration/conversations", response_model=ConversationHistoryListResponse)
def list_orchestration_conversations_route(
    request: Request,
    principal: Annotated[Principal, Depends(require_orchestration_principal)],
) -> ConversationHistoryListResponse:
    """List browser-visible conversations from persisted orchestration state."""

    owner = _resolve_trusted_owner_from_principal(request=request, principal=principal)
    try:
        records = _get_conversation_state_store(request).list_for_user(
            tenant_id=owner["tenant_id"],
            user_id=owner["effective_taxpayer_user_id"],
            limit=1000,
        )
    except ConversationStateStoreError as error:
        raise _http_error(
            request=request,
            status_code=503,
            error_code="conversation_state_persistence_unavailable",
            message=error.message,
            reason=error.reason_code,
            reason_code=error.reason_code,
            context={"tenant_id": owner["tenant_id"]},
        ) from error

    grouped: dict[str, list[ConversationStateRecord]] = defaultdict(list)
    for record in records:
        grouped[record["conversation_id"]].append(record)

    conversations: list[ConversationHistoryConversation] = []
    for conversation_id, conversation_records in grouped.items():
        built = _build_conversation_history_conversation(conversation_id, conversation_records)
        if built is not None:
            conversations.append(built)

    conversations.sort(
        key=lambda conversation: (conversation.updated_at, conversation.conversation_id),
        reverse=True,
    )

    return ConversationHistoryListResponse(
        status="listed",
        service=SERVICE_NAME,
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        conversations=conversations,
    )


@ROUTER.delete("/v1/orchestration/conversations/{conversation_id}", response_model=ConversationDeleteResponse)
def delete_orchestration_conversation_route(
    request: Request,
    conversation_id: str,
    principal: Annotated[Principal, Depends(require_orchestration_principal)],
) -> ConversationDeleteResponse:
    """Delete one scoped conversation-state history record set."""
    owner = _resolve_trusted_owner_from_principal(request=request, principal=principal)
    try:
        deleted_count = _get_conversation_state_store(request).delete(
            tenant_id=owner["tenant_id"],
            conversation_id=conversation_id,
            user_id=owner["effective_taxpayer_user_id"],
        )
    except ConversationStateStoreError as error:
        raise _http_error(
            request=request,
            status_code=503,
            error_code="conversation_state_persistence_unavailable",
            message=error.message,
            reason=error.reason_code,
            reason_code=error.reason_code,
            context={
                "conversation_id": conversation_id,
                "tenant_id": owner["tenant_id"],
            },
        ) from error

    emit_income_tax_audit_event(
        event_type="orchestration_conversation_deleted",
        status="deleted",
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        context={
            "conversation_id": conversation_id,
            "resource_id": conversation_id,
            "deleted_count": deleted_count,
            "tenant_id": owner["tenant_id"],
            "user_id": owner["effective_taxpayer_user_id"],
            "request_path": request.url.path,
            "request_method": request.method,
        },
    )
    return ConversationDeleteResponse(
        status="deleted",
        service=SERVICE_NAME,
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        conversation_id=conversation_id,
        deleted_count=deleted_count,
    )


@ROUTER.patch("/v1/orchestration/conversations/{conversation_id}", response_model=ConversationRenameResponse)
def rename_orchestration_conversation_route(
    request: Request,
    conversation_id: str,
    payload: ConversationRenameRequest,
    principal: Annotated[Principal, Depends(require_orchestration_principal)],
) -> ConversationRenameResponse:
    """Rename one scoped conversation-state history set."""
    owner = _resolve_trusted_owner_from_principal(request=request, principal=principal)
    try:
        updated_count = _get_conversation_state_store(request).rename(
            tenant_id=owner["tenant_id"],
            conversation_id=conversation_id,
            user_id=owner["effective_taxpayer_user_id"],
            conversation_title=payload.conversation_title,
        )
    except ConversationStateStoreError as error:
        raise _http_error(
            request=request,
            status_code=503,
            error_code="conversation_state_persistence_unavailable",
            message=error.message,
            reason=error.reason_code,
            reason_code=error.reason_code,
            context={
                "conversation_id": conversation_id,
                "conversation_title": payload.conversation_title,
                "tenant_id": owner["tenant_id"],
            },
        ) from error

    emit_income_tax_audit_event(
        event_type="orchestration_conversation_renamed",
        status="renamed",
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        context={
            "conversation_id": conversation_id,
            "resource_id": conversation_id,
            "conversation_title": payload.conversation_title,
            "updated_count": updated_count,
            "tenant_id": owner["tenant_id"],
            "user_id": owner["effective_taxpayer_user_id"],
            "request_path": request.url.path,
            "request_method": request.method,
        },
    )
    return ConversationRenameResponse(
        status="renamed",
        service=SERVICE_NAME,
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        conversation_id=conversation_id,
        conversation_title=payload.conversation_title,
        updated_count=updated_count,
    )


@ROUTER.post("/v1/orchestration/conversations/bulk-delete", response_model=BulkConversationDeleteResponse)
def bulk_delete_orchestration_conversations_route(
    request: Request,
    payload: BulkConversationDeleteRequest,
    principal: Annotated[Principal, Depends(require_orchestration_principal)],
) -> BulkConversationDeleteResponse:
    """Delete multiple scoped conversations without leaking cross-tenant existence."""
    owner = _resolve_trusted_owner_from_principal(request=request, principal=principal)
    deleted_conversation_ids: list[str] = []
    deleted_total = 0
    try:
        for conversation_id in payload.conversation_ids:
            deleted_count = _get_conversation_state_store(request).delete(
                tenant_id=owner["tenant_id"],
                conversation_id=conversation_id,
                user_id=owner["effective_taxpayer_user_id"],
            )
            if deleted_count > 0:
                deleted_conversation_ids.append(conversation_id)
                deleted_total += deleted_count
    except ConversationStateStoreError as error:
        raise _http_error(
            request=request,
            status_code=503,
            error_code="conversation_state_persistence_unavailable",
            message=error.message,
            reason=error.reason_code,
            reason_code=error.reason_code,
            context={
                "conversation_ids": payload.conversation_ids,
                "tenant_id": owner["tenant_id"],
            },
        ) from error

    emit_income_tax_audit_event(
        event_type="orchestration_conversations_bulk_deleted",
        status="deleted",
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        context={
            "requested_conversation_ids": payload.conversation_ids,
            "deleted_conversation_ids": deleted_conversation_ids,
            "deleted_count": deleted_total,
            "resource_id": ",".join(payload.conversation_ids),
            "tenant_id": owner["tenant_id"],
            "user_id": owner["effective_taxpayer_user_id"],
            "request_path": request.url.path,
            "request_method": request.method,
        },
    )
    return BulkConversationDeleteResponse(
        status="deleted",
        service=SERVICE_NAME,
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        requested_conversation_ids=payload.conversation_ids,
        deleted_conversation_ids=deleted_conversation_ids,
        deleted_count=deleted_total,
    )


@ROUTER.post("/v1/orchestration/prompt/execute/stream")
def execute_orchestration_prompt_stream_route(
    request: Request,
    payload: PromptExecutionRequest,
    principal: Annotated[Principal, Depends(require_orchestration_principal)],
) -> StreamingResponse:
    """Stream OpenAI answer synthesis deltas for one governed orchestration execution."""

    base_generator = _get_llm_response_generator(request)
    event_queue: queue.Queue[str | object] = queue.Queue()
    request.state.llm_response_generator = _StreamingLLMResponseGeneratorProxy(
        base_generator=base_generator,
        event_queue=event_queue,
    )

    def _run_execution() -> None:
        try:
            response = execute_orchestration_prompt_route(request, payload, principal)
            event_queue.put(
                _format_sse_event(
                    "final",
                    response.model_dump(mode="json"),
                )
            )
        except HTTPException as error:
            detail = (
                cast(dict[str, object], error.detail)
                if isinstance(error.detail, dict)
                else {
                    "error_code": INVALID_ORCHESTRATION_REQUEST,
                    "message": "Orchestration execution failed.",
                    "reason": INVALID_ORCHESTRATION_REQUEST,
                    "reason_code": INVALID_ORCHESTRATION_REQUEST,
                }
            )
            event_queue.put(
                _format_sse_event(
                    "error",
                    {
                        "status_code": error.status_code,
                        "detail": detail,
                    },
                )
            )
        except Exception as error:  # noqa: BLE001
            event_queue.put(
                _format_sse_event(
                    "error",
                    {
                        "status_code": 500,
                        "detail": {
                            "error_code": "stream_execution_failed",
                            "message": "Streaming orchestration execution failed.",
                            "reason": "stream_execution_failed",
                            "reason_code": "stream_execution_failed",
                            "context": {
                                "exception_type": type(error).__name__,
                            },
                        },
                    },
                )
            )
        finally:
            request.state.llm_response_generator = None
            event_queue.put(_STREAM_END)

    def _iterate_events() -> Iterator[str]:
        yield _format_sse_event(
            "start",
            {
                "conversation_id": payload.conversation_id,
                "correlation_id": get_correlation_id(request),
                "trace_id": get_trace_id(request),
            },
        )
        worker = threading.Thread(target=_run_execution, daemon=True)
        worker.start()
        while True:
            next_event = event_queue.get()
            if next_event is _STREAM_END:
                break
            yield cast(str, next_event)

    return StreamingResponse(
        _iterate_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Per-request resolution cache
#
# /decide runs the full LLM envelope + routing and stores the result here.
# /execute retrieves it by prompt_checksum so the LLM is not called again.
#
# Design constraints:
#   - In-process only — no external dependency.
#   - TTL of 120s: decisions only need to survive the decide→execute window.
#   - Max 512 entries: bounds memory under concurrent load. Oldest entry is
#     evicted when the cap is reached (insertion-order dict behaviour).
#   - Thread-safe: a single lock guards reads and writes.
# ---------------------------------------------------------------------------

_RESOLUTION_CACHE_TTL_SECONDS = 120
_RESOLUTION_CACHE_MAX_ENTRIES = 512
_resolution_cache: dict[str, tuple[float, dict[str, object]]] = {}
_resolution_cache_lock = threading.Lock()


def _build_user_context_summary(
    request: Request,
    *,
    tenant_id: str,
) -> UserContextSummary | None:
    if not hasattr(request, "state") or not hasattr(request.state, "user_id"):
        return None
    return UserContextSummary(
        user_id=str(request.state.user_id),
        tenant_id=tenant_id,
        employment_type=_optional_string(getattr(request.state, "employment_type", None)),
        filing_status=_optional_string(getattr(request.state, "filing_status", None)),
        country=_optional_string(getattr(request.state, "country", None)),
        jurisdiction=_optional_string(getattr(request.state, "jurisdiction", None)),
    )


def _resolve_prompt_intent_envelope(
    *,
    request: Request,
    prompt_text: str,
    tenant_id: str,
) -> PromptIntentEnvelope:
    """Legacy envelope helper retained for the income-tax compatibility endpoint.

    It is not used by `/v1/orchestration/prompt/decide`; that endpoint uses
    `build_prompt_intent_envelope_from_turn_resolution` and can remove this
    helper when the compatibility endpoint is retired.
    """
    cached_envelope = getattr(request.state, "resolved_prompt_intent_envelope", None)
    cached_prompt_text = getattr(request.state, "resolved_prompt_intent_prompt_text", None)
    if (
        isinstance(cached_envelope, dict)
        and cached_prompt_text == prompt_text
        and cached_envelope.get("prompt_class") == "income_tax_prompt_flow"
    ):
        emit_orchestration_debug(
            "RUNTIME",
            "execution.cache.hit",
            prompt=bounded_preview(prompt_text),
            correlation_id=get_correlation_id(request),
            trace_id=get_trace_id(request),
        )
        return cast(PromptIntentEnvelope, cached_envelope)

    emit_orchestration_debug(
        "RUNTIME",
        "execution.cache.miss",
        prompt=bounded_preview(prompt_text),
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
    )
    envelope = parse_income_tax_prompt_intent_envelope(
        prompt_text,
        user_context=_build_user_context_summary(request, tenant_id=tenant_id),
        conversation_history=None,
        current_tax_year=2026,
    )
    setattr(request.state, "resolved_prompt_intent_envelope", envelope)
    setattr(request.state, "resolved_prompt_intent_prompt_text", prompt_text)
    return envelope


def _cache_put_resolution(checksum: str, resolution: dict[str, object]) -> None:
    with _resolution_cache_lock:
        if checksum in _resolution_cache:
            # Refresh TTL on re-write (decide called again for same prompt).
            _resolution_cache.pop(checksum)
        elif len(_resolution_cache) >= _RESOLUTION_CACHE_MAX_ENTRIES:
            # Evict oldest entry (first key in insertion-order dict).
            oldest = next(iter(_resolution_cache))
            del _resolution_cache[oldest]
        _resolution_cache[checksum] = (time.monotonic(), resolution)


def _cache_get_resolution(checksum: str) -> dict[str, object] | None:
    with _resolution_cache_lock:
        entry = _resolution_cache.get(checksum)
        if entry is None:
            return None
        stored_at, resolution = entry
        if time.monotonic() - stored_at > _RESOLUTION_CACHE_TTL_SECONDS:
            del _resolution_cache[checksum]
            return None
        return resolution


def _owner_scoped_resolution_key(*, checksum: str, owner: TrustedConversationOwner) -> str:
    """Prevent decision-cache reuse across taxpayers sharing prompt input."""

    return _compute_prompt_decision_id(
        prompt_checksum=checksum,
        intent_class="trusted_conversation_owner",
        plan_id=owner["effective_taxpayer_user_id"],
        tenant_id=owner["tenant_id"],
        conversation_id=owner["effective_taxpayer_user_id"],
    )


def _resolve_prompt_route_decision(
    *,
    request: Request,
    payload: PromptIngestionRequest,
    owner: TrustedConversationOwner,
) -> dict[str, object]:
    install_request_timer()
    prompt_checksum = _compute_prompt_checksum(
        tenant_id=payload.tenant_id,
        conversation_id=payload.conversation_id,
        channel=payload.channel,
        prompt_text=payload.prompt.text,
    )
    emit_orchestration_debug(
        "RUNTIME",
        "decision.start",
        prompt=bounded_preview(payload.prompt.text),
        prompt_checksum=prompt_checksum,
        tenant_id=payload.tenant_id,
        conversation_id=payload.conversation_id,
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
    )
    try:
        timed_print("[DECIDE] About to resolve conversation-state store")
        state_store = _get_conversation_state_store(request)
        timed_print("[DECIDE] Resolved conversation-state store")

        timed_print("[DECIDE] About to load conversation-context configuration")
        conversation_context_config = _get_conversation_context_config(request)
        timed_print(
            "[DECIDE] Loaded conversation-context configuration "
            f"candidate_limit={conversation_context_config.candidate_limit}"
        )

        timed_print("[DECIDE] About to retrieve recent conversation state")
        state = state_store.list_recent(
            tenant_id=payload.tenant_id,
            conversation_id=payload.conversation_id,
            user_id=owner["effective_taxpayer_user_id"],
            limit=conversation_context_config.candidate_limit,
        )
        timed_print(
            "[DECIDE] Retrieved recent conversation state "
            f"record_count={len(state)}"
        )

        timed_print(
            "[DECIDE] About to build bounded conversation candidates "
            f"record_count={len(state)}"
        )
        recent_candidates = build_bounded_candidates(state)
        timed_print(
            "[DECIDE] Built bounded conversation candidates "
            f"candidate_count={len(recent_candidates)}"
        )

        resolver = getattr(request.app.state, "turn_resolver", None)
        if resolver is None:
            raise ConversationTurnResolutionError(
                error_code="conversation_turn_resolver_not_configured",
                reason_code="conversation_turn_resolver_not_configured",
                message="Conversation turn resolver configuration is required.",
            )

        timed_print("[TURN_RESOLVER] About to resolve semantic turn")
        resolver_input = ConversationTurnResolutionInput(
            today=str(date.today()),
            trusted_jurisdiction="Kenya",
            tenant_product_context={
                "tenant_id": payload.tenant_id,
                "conversation_id": payload.conversation_id,
                "effective_taxpayer_user_id": owner["effective_taxpayer_user_id"],
            },
            current_prompt=payload.prompt.text,
            recent_candidates=recent_candidates,
            supported_intents=[
                "lookup_grounded_knowledge",
                "compute_income_tax",
                "compute_health_contribution",
                "generate_form_artifact",
                "generate_report_artifact",
                "meta_conversation",
            ],
            supported_knowledge_domains=sorted(SUPPORTED_ORCHESTRATION_KNOWLEDGE_DOMAINS),
            supported_computations=["compute_income_tax", "compute_health_contribution"],
            supported_artifact_operations=[
                "generate_form_artifact",
                "generate_report_artifact",
            ],
            external_action_considered=False,
            immediately_preceding_clarification=immediately_preceding_clarification(state),
            prior_failure_metadata=None,
        )
        turn_resolution = resolver.resolve_turn(resolver_input)
        timed_print(
            "[TURN_RESOLVER] Resolved semantic turn "
            f"relationship={turn_resolution.relationship.value} "
            f"answerability={turn_resolution.answerability.value}"
        )
        timed_print("[TURN_RESOLVER] About to validate semantic turn resolution")
        validate_conversation_turn_resolution(
            resolution=turn_resolution,
            input_payload=resolver_input,
        )
        timed_print("[TURN_RESOLVER] Validated semantic turn resolution")

        timed_print("[DECIDE] About to project intent envelope from turn resolution")
        current_semantic_frame = build_prompt_intent_envelope_from_turn_resolution(
            turn_resolution=turn_resolution,
            correlation_id=get_correlation_id(request),
            trace_id=get_trace_id(request),
        )
        timed_print(
            "[DECIDE] Projected intent envelope from turn resolution "
            f"intent_class={current_semantic_frame['intent_class']!r} "
            f"tax_domain_hint={current_semantic_frame['tax_domain_hint']!r}"
        )

        timed_print("[DECIDE] About to resolve follow-up context")
        followup_resolution = build_followup_resolution(
            turn_resolution=turn_resolution,
            recent_conversation_state=state,
            current_semantic_frame=current_semantic_frame,
        )
        timed_print(
            "[DECIDE] Resolved follow-up context "
            f"reuse_prior_service_result={followup_resolution.get('reuse_prior_service_result') if followup_resolution is not None else None}"
        )
        prior_stated_facts_record = _select_prior_stated_facts_record(state)
    except (ConversationStateStoreError, ConversationTurnResolutionError) as error:
        reason_code = getattr(error, "reason_code", "conversation_state_unavailable")
        raise OrchestrationRuntimeError(status_code=500, error_code=getattr(error, "error_code", "conversation_turn_resolution_failed"), message="The request could not be processed because conversational resolution is unavailable.", reason=reason_code, reason_code=reason_code, context={"conversation_id": payload.conversation_id}) from error
    intent_envelope = current_semantic_frame
    timed_print("We got the intent envelope...")

    if followup_resolution is not None:
        intent_envelope = cast(
            PromptIntentEnvelope,
            {
                **intent_envelope,
                **followup_resolution.get("updated_semantic_frame", {}),
            },
        )
    effective_prompt_text = turn_resolution.contextualized_prompt
    intent_envelope = cast(PromptIntentEnvelope, {**cast(dict[str, object], intent_envelope), "effective_prompt_text": effective_prompt_text})
    emit_orchestration_debug(
        "RUNTIME",
        "decision.envelope.resolved",
        prompt_checksum=prompt_checksum,
        intent_class=intent_envelope["intent_class"],
        tax_domain_hint=intent_envelope["tax_domain_hint"],
        parsing_status=intent_envelope.get("parsing_status"),
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
    )

    tax_domain_hint = _required_string(intent_envelope, "tax_domain_hint")
    supported_lane_id = _optional_string(intent_envelope.get("requested_lane_hint"))
    historical_version_id = _optional_string(intent_envelope.get("historical_version_hint"))
    tax_year_hint = intent_envelope.get("tax_year_hint")
    correlation_id = _required_string(intent_envelope, "correlation_id")
    timed_print(
        "[ROUTE] "
        f"domain_hint={tax_domain_hint!r}  "
        f"intent_class={intent_envelope['intent_class']!r}  "
        f"parsing_status={intent_envelope.get('parsing_status')!r}  "
        f"lane={intent_envelope.get('requested_lane_hint')!r}  "
        f"version={intent_envelope.get('historical_version_hint')!r}  "
        f"year={intent_envelope.get('tax_year_hint')!r}"
    )
    if tax_domain_hint == "unknown":
        # The rule engine couldn't pin a domain (e.g. "capital gains", "tax
        # return"). If parsing_status signals the prompt is tax-related but
        # unsupported, treat it as a general income_tax knowledge search rather
        # than rejecting outright.
        parsing_status = str(intent_envelope.get("parsing_status", ""))
        intent_class_from_envelope = str(intent_envelope.get("intent_class", ""))
        _llm_confirmed_knowledge = (
            parsing_status == "parsed_with_semantic_extraction"
            and intent_class_from_envelope == "lookup_grounded_knowledge"
        )
        if parsing_status == "parsed_with_unsupported_scope_hint" or _llm_confirmed_knowledge:
            timed_print(
                "[ROUTE] domain_hint=unknown but "
                f"parsing_status={parsing_status!r} "
                f"intent_class={intent_class_from_envelope!r} "
                "→ remapped to income_tax/lookup_grounded_knowledge"
            )
            tax_domain_hint = "income_tax"
            intent_envelope = cast(
                PromptIntentEnvelope,
                {
                    **cast(dict[str, object], intent_envelope),
                    "tax_domain_hint": "income_tax",
                    "intent_class": "lookup_grounded_knowledge",
                },
            )
        else:
            timed_print(
                f"[ROUTE] BLOCK — off_topic_prompt: domain_hint=unknown and "
                f"parsing_status={parsing_status!r} has no recovery path  "
                f"intent_class={intent_class_from_envelope!r}"
            )
            raise _http_error(
                request=request,
                status_code=400,
                error_code=OFF_TOPIC_PROMPT,
                message="Prompt is off-topic for supported orchestration scope.",
                reason=OFF_TOPIC_PROMPT,
                reason_code=OFF_TOPIC_PROMPT,
                context={
                    "prompt_class": intent_envelope["prompt_class"],
                    "tax_domain_hint": tax_domain_hint,
                    "intent_class": intent_class_from_envelope,
                },
            )

    knowledge_route_payload = None
    if turn_resolution.needs_knowledge_retrieval:
        knowledge_route_payload = {"route_mode": "search", "query": effective_prompt_text.strip(), "tax_domain": turn_resolution.retrieval_tax_domain_filter}
    if knowledge_route_payload is not None:
        intent_envelope["knowledge_route_mode_hint"] = str(knowledge_route_payload["route_mode"])

    intent_class = str(intent_envelope["intent_class"])
    clarification: dict[str, object] | None = None

    # A successful semantic resolution, rather than a local parser, is the
    # sole authority for asking the user a clarification question.
    if turn_resolution.answerability.value == "clarification_required":
        intent_class = "clarification_required"
        intent_envelope = cast(PromptIntentEnvelope, {**cast(dict[str, object], intent_envelope), "intent_class": intent_class})

    if intent_class == "unknown" and intent_envelope.get("clarification_reason_code"):
        intent_class = "clarification_required"
        intent_envelope = cast(
            PromptIntentEnvelope,
            {
                **cast(dict[str, object], intent_envelope),
                "intent_class": "clarification_required",
            },
        )

    timed_print(
        f"[ROUTE] routing on intent_class={intent_class!r}  final domain_hint={tax_domain_hint!r}"
    )

    if intent_class == "clarification_required":
        timed_print("Clarification required...")
        plan_payload = build_governed_orchestration_plan(intent_envelope)
        plan_validation = validate_governed_orchestration_plan(
            plan=plan_payload,
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
            for_execution=False,
        )
        if plan_validation["validation_status"] != "accepted":
            error = cast(dict[str, object], plan_validation["error"])
            raise _http_error(
                request=request,
                status_code=400,
                error_code=str(error["error_code"]),
                message=str(error["message"]),
                reason=str(error["reason"]),
                reason_code=str(error.get("reason_code", error["reason"])),
                context=cast(dict[str, object], error.get("rejected_context", {})),
            )
        clarification = {
            "reason_code": intent_envelope.get(
                "clarification_reason_code", "clarification_required"
            ),
            "message": intent_envelope.get(
                "clarification_message",
                "Prompt requires clarification before deterministic planning can continue.",
            ),
            "required_context_fields": list(
                cast(tuple[str, ...], intent_envelope.get("required_context_fields", ()))
            ),
            "candidate_service_families": list(
                cast(tuple[str, ...], intent_envelope.get("candidate_service_families", ()))
            ),
        }
        decision_id = _compute_prompt_decision_id(
            prompt_checksum=prompt_checksum,
            intent_class=intent_class,
            plan_id=str(plan_payload["plan_id"]),
            tenant_id=payload.tenant_id,
            conversation_id=payload.conversation_id,
        )
        return {
            "status": "clarification_required",
            "gate_status": "clarification_required",
            "decision_id": decision_id,
            "prompt_checksum": prompt_checksum,
            "intent_class": intent_class,
            "tax_domain_hint": tax_domain_hint,
            "supported_lane_id": supported_lane_id,
            "historical_version_id": historical_version_id,
            "tax_year": tax_year_hint,
            "regime_identifier": _optional_string(intent_envelope.get("regime_identifier_hint")),
            "knowledge_route_payload": knowledge_route_payload,
            "selected_route": None,
            "plan": plan_payload,
            "clarification": clarification,
            "followup_resolution": followup_resolution,
            "turn_resolution": turn_resolution.model_dump(mode="python"),
            "semantic_turn_resolution_call_count": 1,
            "prior_stated_facts_record": prior_stated_facts_record,
            "stated_facts": intent_envelope.get("stated_facts", {}),
        }

    if intent_class == "compute_income_tax":
        timed_print("We are computing income tax")
        default_lane_context = _default_income_tax_computation_lane_context()
        missing_lane_context = [
            field_name
            for field_name, field_value in (
                ("supported_lane_id", supported_lane_id),
                ("historical_version_id", historical_version_id),
                ("tax_year", tax_year_hint),
            )
            if field_value is None
        ]
        if default_lane_context is not None:
            default_supported_lane_id, default_historical_version_id, default_tax_year = (
                default_lane_context
            )
            current_lane_supported = (
                supported_lane_id is not None
                and historical_version_id is not None
                and tax_year_hint is not None
            )
            if current_lane_supported:
                try:
                    manifest = load_income_tax_vertical_slice_manifest()
                    supported_lanes = manifest.get("supported_lanes")
                    if not isinstance(supported_lanes, list):
                        current_lane_supported = False
                    else:
                        current_lane_supported = any(
                            isinstance(lane, dict)
                            and lane.get("supported_lane_id") == supported_lane_id
                            and lane.get("historical_version_id") == historical_version_id
                            and lane.get("tax_year") == tax_year_hint
                            and lane.get("status") == "supported"
                            for lane in supported_lanes
                        )
                except Exception:
                    current_lane_supported = False
            if not current_lane_supported:
                supported_lane_id = default_supported_lane_id
                historical_version_id = default_historical_version_id
                tax_year_hint = default_tax_year
                missing_lane_context = []
        if missing_lane_context:
            clarification_message = (
                "Please tell me which governed income-tax lane, regime "
                "version, and tax year should be used."
            )
            clarification = {
                "reason_code": "missing_lane_context",
                "message": clarification_message,
                "required_context_fields": missing_lane_context,
                "candidate_service_families": ["compute_income_tax"],
            }
            clarification_plan_id = _compute_prompt_decision_id(
                prompt_checksum=prompt_checksum,
                intent_class="clarification_required",
                plan_id="missing_lane_context",
                tenant_id=payload.tenant_id,
                conversation_id=payload.conversation_id,
            )
            clarification_decision_id = _compute_prompt_decision_id(
                prompt_checksum=prompt_checksum,
                intent_class="clarification_required",
                plan_id=clarification_plan_id,
                tenant_id=payload.tenant_id,
                conversation_id=payload.conversation_id,
            )
            return {
                "status": "clarification_required",
                "gate_status": "clarification_required",
                "decision_id": clarification_decision_id,
                "prompt_checksum": prompt_checksum,
                "intent_class": "clarification_required",
                "tax_domain_hint": tax_domain_hint,
                "supported_lane_id": supported_lane_id,
                "historical_version_id": historical_version_id,
                "tax_year": tax_year_hint,
                "regime_identifier": _optional_string(intent_envelope.get("regime_identifier_hint")),
                "knowledge_route_payload": knowledge_route_payload,
                "selected_route": None,
                "plan": {
                    "plan_id": clarification_plan_id,
                    "plan_version": "2.0.0",
                    "plan_status": "clarification_required",
                    "planning_mode": "clarification_required",
                    "execution_ready": False,
                    "steps": [],
                },
                "clarification": clarification,
                "followup_resolution": followup_resolution,
                "turn_resolution": turn_resolution.model_dump(mode="python"),
                "semantic_turn_resolution_call_count": 1,
                "prior_stated_facts_record": prior_stated_facts_record,
                "stated_facts": intent_envelope.get("stated_facts", {}),
            }
        else:
            try:
                enforce_income_tax_runtime_capability_gate(
                    prompt_text=effective_prompt_text,
                    supported_lane_id=supported_lane_id,
                    historical_version_id=historical_version_id,
                    tax_year=tax_year_hint,
                    correlation_id=correlation_id,
                    tenant_id=payload.tenant_id,
                )
            except IncomeTaxCapabilityGateError as error:
                payload_detail = error.payload()
                reason_code = str(payload_detail.get("reason_code", payload_detail["reason"]))
                status_code = (
                    403 if str(payload_detail["error_code"]) == "pilot_tenant_not_allowed" else 404
                )
                raise _http_error(
                    request=request,
                    status_code=status_code,
                    error_code=str(payload_detail["error_code"]),
                    message=str(payload_detail["message"]),
                    reason=str(payload_detail["reason"]),
                    reason_code=reason_code,
                    context=cast(
                        dict[str, object],
                        payload_detail.get("rejected_context", {}),
                    ),
                ) from error
    elif intent_class == "compute_health_contribution":
        timed_print("We are at compute health contribution")
        _enforce_health_contribution_runtime_scope(
            request=request,
            payload=payload,
            intent_envelope=intent_envelope,
        )
    elif intent_class in {
        "lookup_grounded_knowledge",
        "retrieve_grounded_knowledge",
    }:
        timed_print(f"[ROUTE] knowledge route: domain_hint={tax_domain_hint!r}")
        # No domain remap — every domain that has a template in _GOVERNED_STEP_TEMPLATES
        # is valid as-is. Forcing everything to income_tax hides real domain context and
        # causes cross_domain_leakage in the validator. Unknown/unrecognised domains
        # are caught by the unsupported_intent_plan path in build_governed_orchestration_plan.
        if knowledge_route_payload is None:
            timed_print(
                f"[ROUTE] knowledge route: no structured payload from extractor "
                f"— building fallback from prompt text  domain_hint={tax_domain_hint!r}"
            )
            knowledge_route_payload = {
                "route_mode": "search",
                "query": effective_prompt_text.strip(),
                "tax_domain": tax_domain_hint,
            }
            intent_envelope["knowledge_route_mode_hint"] = "search"
    elif intent_class in {
        "generate_form_artifact",
        "generate_report_artifact",
        "extract_document",
        "compute_plus_grounding",
    }:
        pass
    elif intent_class == "meta_conversation":
        pass
    elif intent_class == "unsupported_domain_request":
        # Domain is recognised as tax-related but has no compute route.
        # Treat it as a knowledge search so the user still gets an answer.
        intent_class = "lookup_grounded_knowledge"
        intent_envelope = cast(
            PromptIntentEnvelope,
            {
                **cast(dict[str, object], intent_envelope),
                "intent_class": "lookup_grounded_knowledge",
            },
        )
        timed_print(
            "[ROUTE] unsupported_domain_request → remapped to "
            "lookup_grounded_knowledge  "
            f"domain_hint={tax_domain_hint!r}"
        )
        if knowledge_route_payload is None:
            knowledge_route_payload = {
                "route_mode": "search",
                "query": effective_prompt_text.strip(),
                "tax_domain": tax_domain_hint,
            }
    else:
        timed_print(
            f"[ROUTE] BLOCK — unsupported_domain: intent_class={intent_class!r} fell through all "
            f"routing branches  domain_hint={tax_domain_hint!r}"
        )
        raise _http_error(
            request=request,
            status_code=400,
            error_code=UNSUPPORTED_PROMPT_SCOPE,
            message="Prompt scope is not supported by deterministic orchestration boundary.",
            reason="unsupported_domain",
            reason_code="unsupported_domain",
            context={
                "prompt_class": intent_envelope["prompt_class"],
                "tax_domain_hint": tax_domain_hint,
                "intent_class": intent_envelope["intent_class"],
            },
        )

    if knowledge_route_payload is not None and followup_resolution is not None:
        knowledge_route_payload = _apply_followup_knowledge_context(
            route_payload=cast(dict[str, object], knowledge_route_payload),
            followup_resolution=followup_resolution,
            tax_domain_hint=tax_domain_hint,
        )

    timed_print("We are now going to build governed orchestration plan...using intent envelope")
    try:
        plan_payload = build_governed_orchestration_plan(intent_envelope)
    except IntentToPlanError as error:
        timed_print("An error occured while building it")
        payload_detail = error.payload()
        _persist_failed_turn_outcome(
            request=request,
            payload=payload,
            user_id=owner["effective_taxpayer_user_id"],
            prompt_text=effective_prompt_text,
            prompt_checksum=prompt_checksum,
            error_code=str(payload_detail["error_code"]),
            reason_code=str(payload_detail["reason"]),
            message=str(payload_detail["message"]),
            stage="plan_build",
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
        )
        raise _http_error(
            request=request,
            status_code=400,
            error_code=str(payload_detail["error_code"]),
            message=str(payload_detail["message"]),
            reason=str(payload_detail["reason"]),
            reason_code=str(payload_detail["reason"]),
            context=cast(dict[str, object], payload_detail.get("rejected_context", {})),
        ) from error
    plan_payload = _resolve_execution_capable_plan(
        plan_payload=cast(dict[str, object], plan_payload),
        intent_class=intent_class,
        tax_domain_hint=tax_domain_hint,
    )
    plan_validation = validate_governed_orchestration_plan(
        plan=plan_payload,
        intent_class=intent_class,
        tax_domain_hint=tax_domain_hint,
        for_execution=False,
    )
    timed_print("We are here...")
    if plan_validation["validation_status"] != "accepted":
        timed_print("Plan was not accepted")
        error = cast(dict[str, object], plan_validation["error"])
        timed_print(f"plan_error={dict(error.items())}")
        _persist_failed_turn_outcome(
            request=request,
            payload=payload,
            user_id=owner["effective_taxpayer_user_id"],
            prompt_text=effective_prompt_text,
            prompt_checksum=prompt_checksum,
            error_code=str(error["error_code"]),
            reason_code=str(error["reason"]),
            message=str(error["message"]),
            stage="plan_validation",
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
        )
        raise _http_error(
            request=request,
            status_code=400,
            error_code=str(error["error_code"]),
            message=str(error["message"]),
            reason=str(error["reason"]),
            reason_code=str(error.get("reason_code", error["reason"])),
            context=cast(dict[str, object], error.get("rejected_context", {})),
        )

    selected_route_payload = extract_selected_route_from_governed_plan(
        cast(GovernedOrchestrationPlan, plan_payload)
    )
    timed_print("We have extracted selected route from governed plan")
    selected_route = (
        OrchestrationRouteSelection.model_validate(selected_route_payload)
        if selected_route_payload is not None
        else None
    )
    emit_orchestration_debug(
        "RUNTIME",
        "decision.route.selected",
        prompt_checksum=prompt_checksum,
        route_id=_optional_string(selected_route_payload.get("route_id"))
        if isinstance(selected_route_payload, dict)
        else None,
        target_service=_optional_string(selected_route_payload.get("target_service"))
        if isinstance(selected_route_payload, dict)
        else None,
        target_operation=_optional_string(selected_route_payload.get("target_operation"))
        if isinstance(selected_route_payload, dict)
        else None,
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
    )
    gate_status = "allowed" if bool(plan_payload["execution_ready"]) else "plan_only"
    decision_id = _compute_prompt_decision_id(
        prompt_checksum=prompt_checksum,
        intent_class=intent_class,
        plan_id=str(plan_payload["plan_id"]),
        tenant_id=payload.tenant_id,
        conversation_id=payload.conversation_id,
    )
    timed_print("We are returning results")
    emit_orchestration_debug(
        "RUNTIME",
        "decision.completed",
        prompt_checksum=prompt_checksum,
        decision_id=decision_id,
        intent_class=intent_class,
        tax_domain_hint=tax_domain_hint,
        gate_status=gate_status,
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
    )
    return {
        "status": "resolved",
        "gate_status": gate_status,
        "decision_id": decision_id,
        "prompt_checksum": prompt_checksum,
        "intent_class": intent_class,
        "tax_domain_hint": tax_domain_hint,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year_hint,
        "regime_identifier": _optional_string(intent_envelope.get("regime_identifier_hint")),
        "knowledge_route_payload": knowledge_route_payload,
        "selected_route": selected_route,
        "plan": plan_payload,
        "clarification": clarification,
        "followup_resolution": followup_resolution,
        "turn_resolution": turn_resolution.model_dump(mode="python"),
        "semantic_turn_resolution_call_count": 1,
        "prior_stated_facts_record": prior_stated_facts_record,
        "stated_facts": intent_envelope.get("stated_facts", {}),
    }


def _enforce_health_contribution_runtime_scope(
    *,
    request: Request,
    payload: PromptIngestionRequest,
    intent_envelope: Mapping[str, object],
) -> None:
    supported_lane_id = _optional_string(intent_envelope.get("requested_lane_hint"))
    timed_print(
        f"[HEALTH_SCOPE] checking: lane={supported_lane_id!r}  "
        f"version={intent_envelope.get('historical_version_hint')!r}  "
        f"year={intent_envelope.get('tax_year_hint')!r}  "
        f"regime={intent_envelope.get('regime_identifier_hint')!r}"
    )
    historical_version_id = _optional_string(intent_envelope.get("historical_version_hint"))
    regime_identifier = _optional_string(intent_envelope.get("regime_identifier_hint"))
    tax_year = intent_envelope.get("tax_year_hint")
    context = {
        "prompt_class": intent_envelope["prompt_class"],
        "tax_domain_hint": intent_envelope["tax_domain_hint"],
        "intent_class": intent_envelope["intent_class"],
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "regime_identifier": regime_identifier,
        "tenant_id": payload.tenant_id,
    }
    if historical_version_id is None or not isinstance(tax_year, int) or regime_identifier is None:
        missing = [
            name
            for name, val in (
                ("historical_version_id", historical_version_id),
                ("tax_year", tax_year if isinstance(tax_year, int) else None),
                ("regime_identifier", regime_identifier),
            )
            if val is None
        ]
        timed_print(f"[HEALTH_SCOPE] BLOCK — missing_health_lane_context: missing={missing}")
        raise _http_error(
            request=request,
            status_code=404,
            error_code=UNSUPPORTED_PROMPT_SCOPE,
            message=(
                "Health-contribution prompt context does not contain governed routing identity."
            ),
            reason="missing_health_lane_context",
            reason_code="missing_health_lane_context",
            context=context,
        )
    if supported_lane_id is None:
        reason_code = (
            "unsupported_health_transition_window"
            if regime_identifier == "transition_boundary"
            else "unsupported_health_version_window"
        )
        if historical_version_id in {
            "HCH-VER-20240701-A",
            "HCH-VER-20240920-PIT",
        }:
            reason_code = "unresolved_health_transition_window"
        timed_print(
            f"[HEALTH_SCOPE] BLOCK — {reason_code}: supported_lane_id=None  "
            f"version={historical_version_id!r}  regime={regime_identifier!r}"
        )
        raise _http_error(
            request=request,
            status_code=404,
            error_code=UNSUPPORTED_PROMPT_SCOPE,
            message=(
                "Health-contribution prompt scope is outside governed supported runtime windows."
            ),
            reason=reason_code,
            reason_code=reason_code,
            context=context,
        )
    expected_lane_id = SUPPORTED_HEALTH_ROUTING_CONTEXTS.get(
        (regime_identifier, historical_version_id)
    )
    if expected_lane_id != supported_lane_id:
        reason_code = "unsupported_health_version_window"
        if historical_version_id in KNOWN_NON_READY_HEALTH_WINDOWS:
            reason_code = "unsupported_health_version_window"
        timed_print(
            f"[HEALTH_SCOPE] BLOCK — {reason_code}: expected_lane={expected_lane_id!r} "
            f"!= actual_lane={supported_lane_id!r}  version={historical_version_id!r}"
        )
        raise _http_error(
            request=request,
            status_code=404,
            error_code=UNSUPPORTED_PROMPT_SCOPE,
            message=(
                "Health-contribution prompt scope is outside governed supported runtime windows."
            ),
            reason=reason_code,
            reason_code=reason_code,
            context=context,
        )


def _resolve_allowed_execution_route(
    *,
    tax_domain_hint: str,
    selected_route: OrchestrationRouteSelection,
) -> OrchestrationRouteSelection | None:
    candidates = ALLOWED_EXECUTION_ROUTES_BY_DOMAIN.get(tax_domain_hint, {})
    expected_route = candidates.get(selected_route.route_id)
    if expected_route is None:
        return None
    if (
        expected_route.target_service != selected_route.target_service
        or expected_route.target_operation != selected_route.target_operation
    ):
        return None
    return expected_route


def _resolve_execution_capable_plan(
    *,
    plan_payload: dict[str, object],
    intent_class: str,
    tax_domain_hint: str,
) -> dict[str, object]:
    if (
        intent_class != "compute_plus_grounding"
        or tax_domain_hint not in {"income_tax", "health_contribution"}
        or plan_payload.get("planning_mode") != "multi_step"
    ):
        return plan_payload
    steps = plan_payload.get("steps")
    if not isinstance(steps, list):
        return plan_payload
    typed_steps = cast(list[object], steps)
    if len(typed_steps) != 2:
        return plan_payload
    return {
        **plan_payload,
        "execution_ready": True,
        "steps": [
            {
                **cast(dict[str, object], step),
                "step_status": "planned",
            }
            for step in typed_steps
            if isinstance(step, dict)
        ],
    }


def _resolve_execution_knowledge_route_payload(
    *,
    prompt_text: str,
    tax_domain_hint: str,
    intent_class: str,
    route_payload: dict[str, object] | None,
) -> dict[str, object] | None:
    if route_payload is not None:
        return route_payload
    if intent_class != "compute_plus_grounding":
        return None
    inferred = extract_knowledge_route_payload(prompt_text)
    if inferred is not None and inferred.get("route_mode") != "retrieve":
        payload = dict(inferred)
        payload["tax_domain"] = tax_domain_hint
        payload.setdefault("source_type", "tax_law")
        return payload
    normalized_prompt = " ".join(prompt_text.strip().split())
    query = normalized_prompt
    for marker in (
        " with legal basis",
        " with statutory authority",
        " with legal authority",
    ):
        if marker in query.lower():
            split_index = query.lower().rfind(marker)
            query = query[:split_index]
            break
    query = query.strip(" .")
    if not query:
        return None
    return {
        "route_mode": "search",
        "query": query,
        "source_type": "tax_law",
        "tax_domain": tax_domain_hint,
        "effective_date": None,
    }


def _apply_followup_knowledge_context(
    *,
    route_payload: dict[str, object],
    followup_resolution: FollowupResolutionResult,
    tax_domain_hint: str,
) -> dict[str, object]:
    """Preserve a semantic referent through planning into the retrieval call."""
    entity = _optional_string(followup_resolution.get("resolved_entity"))
    if entity is None:
        return route_payload
    enriched = dict(route_payload)
    enriched["resolved_entity"] = entity
    enriched["tax_domain"] = str(followup_resolution.get("resolved_tax_domain", tax_domain_hint))
    query = _optional_string(enriched.get("query"))
    if query is not None and entity.lower() not in query.lower():
        enriched["query"] = f"{query} {entity} Kenya"
    return enriched


def _default_income_tax_computation_lane_context() -> tuple[str, str, int] | None:
    """Return the latest governed resident-employment lane context when one is needed."""

    try:
        manifest = load_income_tax_vertical_slice_manifest()
    except Exception:
        return None
    supported_lanes = manifest.get("supported_lanes")
    if not isinstance(supported_lanes, list):
        return None
    resident_lanes: list[tuple[int, dict[str, object]]] = []
    for lane in supported_lanes:
        if not isinstance(lane, dict):
            continue
        lane_id = lane.get("supported_lane_id")
        historical_version_id = lane.get("historical_version_id")
        tax_year = lane.get("tax_year")
        status = lane.get("status")
        if (
            status == "supported"
            and isinstance(lane_id, str)
            and lane_id.startswith("resident_employment_income_")
            and isinstance(historical_version_id, str)
            and isinstance(tax_year, int)
        ):
            resident_lanes.append((tax_year, lane))
    if not resident_lanes:
        return None
    _, selected_lane = max(
        resident_lanes,
        key=lambda item: (
            item[0],
            str(item[1].get("historical_version_id", "")),
            str(item[1].get("supported_lane_id", "")),
        ),
    )
    return (
        str(selected_lane["supported_lane_id"]),
        str(selected_lane["historical_version_id"]),
        int(selected_lane["tax_year"]),
    )


def _status_code_for_route_failure(*, target_service: str, reason_code: str) -> int:
    if target_service == "knowledge":
        if reason_code in {
            "unsupported_knowledge_scope",
            "invalid_knowledge_identifier",
        }:
            return 404
        if reason_code in {
            "invalid_knowledge_lineage",
            "insufficient_grounded_evidence",
        }:
            return 409
        return 502
    return (
        404
        if reason_code
        in {
            "missing_route_target",
            "unsupported_route_target",
            "route_action_mismatch",
            "unsupported_action_type",
        }
        else 502
    )


def _first_non_resolved_step(
    *,
    step_results: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    for step in step_results:
        if step.get("step_status") != "resolved":
            return dict(step)
    return dict(step_results[0])


def _raise_for_existing_execution_idempotency_conflict(
    *,
    request: Request,
    payload: PromptExecutionRequest,
    prompt_checksum: str,
    route_payload: dict[str, object] | None,
) -> None:
    store = get_default_action_execution_idempotency_store()
    try:
        existing = store.get(payload.idempotency_key)
    except ActionExecutionStoreError:
        return
    if existing is None:
        return
    action_context = existing["envelope"].get("action_context")
    if route_payload is None:
        if not isinstance(action_context, Mapping):
            return
        stored_prompt_checksum = _optional_string(
            cast(Mapping[str, object], action_context).get("submission_payload_ref")
        )
        if stored_prompt_checksum is None or stored_prompt_checksum == prompt_checksum:
            return
        raise _http_error(
            request=request,
            status_code=400,
            error_code=INVALID_ORCHESTRATION_REQUEST,
            message="Execution context does not match deterministic prompt classification.",
            reason="prompt_context_mismatch",
            reason_code="prompt_context_mismatch",
            context={
                "idempotency_key": payload.idempotency_key,
                "decision_id": payload.decision_id,
            },
        )
    if not _stored_execution_conflicts_with_prompt_request(
        request=request,
        payload=payload,
        prompt_checksum=prompt_checksum,
        route_payload=route_payload,
        existing=existing["envelope"],
        stored_request_fingerprint=existing["request_fingerprint"],
    ):
        return
    raise _http_error(
        request=request,
        status_code=400,
        error_code=INVALID_ORCHESTRATION_REQUEST,
        message="Execution context does not match deterministic prompt classification.",
        reason="prompt_context_mismatch",
        reason_code="prompt_context_mismatch",
        context={
            "idempotency_key": payload.idempotency_key,
            "decision_id": payload.decision_id,
        },
    )


def _stored_execution_conflicts_with_prompt_request(
    *,
    request: Request,
    payload: PromptExecutionRequest,
    prompt_checksum: str,
    route_payload: dict[str, object] | None,
    existing: Mapping[str, object],
    stored_request_fingerprint: str,
) -> bool:
    action_context = existing.get("action_context")
    if not isinstance(action_context, Mapping):
        return True
    normalized_action_context = cast(Mapping[str, object], action_context)
    target_service = _optional_string(normalized_action_context.get("target_service"))
    if target_service == "knowledge":
        return False
    action_type = _optional_string(normalized_action_context.get("action_type"))
    if action_type is None:
        return True
    tax_year_value = normalized_action_context.get("tax_year")
    candidate_request: ActionExecutionRequest = {
        "idempotency_key": payload.idempotency_key,
        "correlation_id": get_correlation_id(request),
        "action_type": action_type,
        "submission_payload_ref": prompt_checksum,
        "capability_context": {
            "supported_lane_id": _optional_string(
                normalized_action_context.get("supported_lane_id")
            ),
            "historical_version_id": _optional_string(
                normalized_action_context.get("historical_version_id")
            ),
            "tax_year": (tax_year_value if isinstance(tax_year_value, int) else None),
        },
        "auth_context": {
            "tenant_id": payload.tenant_id,
            "user_id": payload.effective_taxpayer_user_id,
        },
    }
    for field_name in (
        "route_id",
        "target_service",
        "target_operation",
        "plan_id",
        "step_id",
    ):
        field_value = _optional_string(normalized_action_context.get(field_name))
        if field_value is not None:
            candidate_request[field_name] = field_value
    if route_payload is not None:
        candidate_request["route_payload"] = route_payload
    candidate_fingerprint = build_action_execution_request_fingerprint(candidate_request)
    return candidate_fingerprint != stored_request_fingerprint


@ROUTER.api_route(
    "/v1/orchestration/{scope}/{remaining_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
def orchestration_scope_guard(
    request: Request,
    scope: str,
    remaining_path: str,
) -> dict[str, object]:
    """Fail closed for unsupported orchestration scope paths."""

    _ = (scope, remaining_path)
    raise _http_error(
        request=request,
        status_code=404,
        error_code=UNSUPPORTED_ORCHESTRATION_SCOPE,
        message="Requested orchestration scope is not supported.",
        reason=UNSUPPORTED_ORCHESTRATION_SCOPE,
        reason_code=UNSUPPORTED_ORCHESTRATION_SCOPE,
    )


def _required_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    raise HTTPException(
        status_code=400,
        detail={
            "error_code": INVALID_ORCHESTRATION_REQUEST,
            "message": f"Orchestration request field `{field_name}` is invalid.",
            "reason": INVALID_ORCHESTRATION_REQUEST,
            "reason_code": INVALID_ORCHESTRATION_REQUEST,
        },
    )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _compute_prompt_checksum(
    *,
    tenant_id: str,
    conversation_id: str,
    channel: str,
    prompt_text: str,
) -> str:
    canonical_payload = {
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "channel": channel,
        "prompt_text": prompt_text,
    }
    encoded = json.dumps(canonical_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compute_prompt_decision_id(
    *,
    prompt_checksum: str,
    intent_class: str,
    plan_id: str,
    tenant_id: str,
    conversation_id: str,
) -> str:
    canonical_payload = {
        "prompt_checksum": prompt_checksum,
        "intent_class": intent_class,
        "plan_id": plan_id,
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
    }
    encoded = json.dumps(canonical_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_object(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": INVALID_ORCHESTRATION_REQUEST,
                "message": "Orchestration request payload is invalid.",
                "reason": INVALID_ORCHESTRATION_REQUEST,
                "reason_code": INVALID_ORCHESTRATION_REQUEST,
            },
        )
    source = cast(Mapping[object, object], payload)
    return {str(key): source[key] for key in source}


def _get_knowledge_repository(
    request: Request,
) -> KnowledgeRouteRepositoryProtocol | None:
    repository = getattr(request.app.state, "knowledge_repository", None)
    if repository is not None:
        return cast(KnowledgeRouteRepositoryProtocol, repository)
    try:
        return cast(KnowledgeRouteRepositoryProtocol, get_default_knowledge_repository())
    except KnowledgeRepositoryError:
        return None


def _get_knowledge_repository_from_app(app: FastAPI) -> KnowledgeRouteRepositoryProtocol | None:
    """Resolve the app's governed knowledge repository for bounded synthesis tools."""

    repository = getattr(app.state, "knowledge_repository", None)
    if repository is not None:
        return cast(KnowledgeRouteRepositoryProtocol, repository)
    try:
        return cast(KnowledgeRouteRepositoryProtocol, get_default_knowledge_repository())
    except KnowledgeRepositoryError:
        return None


def _get_llm_response_generator(
    request: Request,
) -> LLMResponseGeneratorProtocol:
    generator = getattr(request.state, "llm_response_generator", None)
    if generator is not None:
        return cast(LLMResponseGeneratorProtocol, generator)
    generator = getattr(request.app.state, "llm_response_generator", None)
    if generator is None:
        return build_default_llm_response_generator()
    return cast(LLMResponseGeneratorProtocol, generator)


def _format_sse_event(event_name: str, data: object) -> str:
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}\n\n"
    )


def _get_conversation_state_store(request: Request) -> ConversationStateStore:
    store = getattr(request.app.state, "conversation_state_store", None)
    if store is None:
        return build_default_conversation_state_store()
    return cast(ConversationStateStore, store)


def _get_conversation_state_store_from_app(app: FastAPI) -> ConversationStateStore:
    store = getattr(app.state, "conversation_state_store", None)
    if store is None:
        return build_default_conversation_state_store()
    return cast(ConversationStateStore, store)


def _get_conversation_state_protector(request: Request) -> ConversationStateProtector | None:
    configured = getattr(request.app.state, "conversation_state_protector", None)
    if configured is not None:
        return cast(ConversationStateProtector, configured)
    return build_default_conversation_state_protector()


def _get_conversation_context_config(
    request: Request,
) -> OrchestrationConversationContextConfig:
    config = getattr(request.app.state, "conversation_context_config", None)
    if config is None:
        return load_orchestration_conversation_context_config()
    return cast(OrchestrationConversationContextConfig, config)


def _get_runtime_rollout_config(
    request: Request,
) -> OrchestrationRuntimeRolloutConfig:
    config = getattr(request.app.state, "runtime_rollout_config", None)
    if config is None:
        return load_orchestration_runtime_rollout_config()
    return cast(OrchestrationRuntimeRolloutConfig, config)


def _evaluate_orchestration_feature_gate(
    *,
    request: Request,
    tenant_id: str,
    feature_key: str,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
) -> tuple[str, str] | None:
    rollout_config = _get_runtime_rollout_config(request)
    if feature_key == "response_synthesis" and not rollout_config.response_synthesis_enabled:
        return (
            "response_synthesis_disabled",
            "OpenAI response synthesis is disabled by orchestration rollout control.",
        )
    if (
        feature_key == "conversation_continuity"
        and not rollout_config.conversation_continuity_enabled
    ):
        return (
            "conversation_continuity_disabled",
            "Bounded same-conversation continuity is disabled by orchestration rollout control.",
        )
    tenant_decision = evaluate_orchestration_pilot_tenant_feature(
        tenant_id=tenant_id,
        feature_key=feature_key,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        correlation_id=get_correlation_id(request),
    )
    if tenant_decision["guard_status"] != "allowed":
        return (tenant_decision["reason_code"], tenant_decision["reason"])
    safety_decision = evaluate_orchestration_feature_safety_controls(
        feature_key=feature_key,
        correlation_id=get_correlation_id(request),
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
    )
    if safety_decision["control_status"] != "allowed":
        return (safety_decision["reason_code"], safety_decision["reason"])
    return None


def _select_prior_stated_facts_record(
    records: Sequence[ConversationStateRecord],
) -> ConversationStateRecord | None:
    """Return the newest persisted record that carries protected taxpayer facts."""

    for record in records:
        stated_facts = record["context_payload"].get("stated_facts")
        if isinstance(stated_facts, Mapping) and stated_facts:
            return record
    return None


def _build_followup_context_execution(
    *,
    request: Request,
    payload: PromptExecutionRequest,
    resolution: Mapping[str, object],
    followup_resolution: FollowupResolutionResult,
    selected_route: OrchestrationRouteSelection,
    prompt_checksum: str,
) -> FollowupContextExecutionResult:
    provider_reference = _first_available_reference(
        followup_resolution.get("reused_service_result_payload")
    )
    adapter_result_payload = followup_resolution.get("reused_service_result_payload") or {}
    adapter_response: dict[str, object] = {
        "adapter_status": "accepted",
        "provider_reference": provider_reference,
        "action_result_code": "conversation_context_resolved",
        "message": "Follow-up answer resolved from prior governed conversation context.",
        "trace": {
            "correlation_id": get_correlation_id(request),
            "trace_id": get_trace_id(request),
            "adapter_request_id": _compute_prompt_decision_id(
                prompt_checksum=prompt_checksum,
                intent_class=followup_resolution["followup_mode"],
                plan_id=followup_resolution["prior_execution_id"],
                tenant_id=payload.tenant_id,
                conversation_id=payload.conversation_id,
            ),
            "adapter_name": "deterministic_conversation_context_adapter_v1",
            "submission_payload_ref": prompt_checksum,
            "idempotency_key": payload.idempotency_key,
            "route_id": selected_route.route_id,
            "target_service": selected_route.target_service,
            "target_operation": selected_route.target_operation,
            "plan_id": str(cast(Mapping[str, object], resolution["plan"]).get("plan_id")),
        },
        "error": None,
        "result_payload": adapter_result_payload,
    }
    return {
        "execution_id": _compute_prompt_decision_id(
            prompt_checksum=prompt_checksum,
            intent_class=followup_resolution["followup_mode"],
            plan_id=followup_resolution["prior_execution_id"],
            tenant_id=payload.tenant_id,
            conversation_id=payload.conversation_id,
        ),
        "plan": cast(dict[str, object], resolution["plan"]),
        "mapped_result": {
            "action_status": "accepted",
            "reason_code": "conversation_context_resolved",
            "reason": "Follow-up answer resolved from prior governed conversation context.",
            "retryable": False,
            "next_retry_at": None,
            "provider_reference": provider_reference,
            "correlation_id": get_correlation_id(request),
            "idempotency_key": payload.idempotency_key,
            "trace_id": get_trace_id(request),
        },
        "adapter_response": adapter_response,
    }


def _build_meta_conversation_execution(
    *,
    request: Request,
    payload: PromptExecutionRequest,
    resolution: Mapping[str, object],
    followup_resolution: FollowupResolutionResult | None,
    selected_route: OrchestrationRouteSelection,
    prompt_checksum: str,
) -> FollowupContextExecutionResult:
    turn_resolution = cast(Mapping[str, object], resolution.get("turn_resolution") or {})
    context_summary = (
        followup_resolution.get("conversation_context_summary")
        if followup_resolution is not None
        else {}
    )
    meta_payload: dict[str, object] = {
        "current_meta_prompt": turn_resolution.get("contextualized_prompt"),
        "relationship": turn_resolution.get("relationship"),
        "user_feedback_intent": turn_resolution.get("intent_class"),
        "tax_computation_payload": None,
        "prior_failure_summary": (
            context_summary.get("prior_failure_summary")
            if isinstance(context_summary, Mapping)
            else None
        ),
        "previous_answer_summary": (
            context_summary.get("prior_answer_summary")
            if isinstance(context_summary, Mapping)
            else None
        ),
    }
    adapter_response: dict[str, object] = {
        "adapter_status": "accepted",
        "provider_reference": None,
        "action_result_code": "meta_conversation_resolved",
        "message": "Meta conversation resolved by orchestration.",
        "trace": {
            "correlation_id": get_correlation_id(request),
            "trace_id": get_trace_id(request),
            "adapter_request_id": _compute_prompt_decision_id(
                prompt_checksum=prompt_checksum,
                intent_class="meta_conversation",
                plan_id=str(resolution["plan"]["plan_id"]),
                tenant_id=payload.tenant_id,
                conversation_id=payload.conversation_id,
            ),
            "adapter_name": "deterministic_meta_conversation_adapter_v1",
            "submission_payload_ref": prompt_checksum,
            "idempotency_key": payload.idempotency_key,
            "route_id": selected_route.route_id,
            "target_service": selected_route.target_service,
            "target_operation": selected_route.target_operation,
            "plan_id": str(cast(Mapping[str, object], resolution["plan"]).get("plan_id")),
        },
        "error": None,
        "result_payload": meta_payload,
    }
    execution_id = _compute_prompt_decision_id(
        prompt_checksum=prompt_checksum,
        intent_class="meta_conversation",
        plan_id=str(resolution["plan"]["plan_id"]),
        tenant_id=payload.tenant_id,
        conversation_id=payload.conversation_id,
    )
    return {
        "execution_id": execution_id,
        "plan": cast(dict[str, object], resolution["plan"]),
        "mapped_result": {
            "action_status": "accepted",
            "reason_code": "meta_conversation_resolved",
            "reason": "Meta conversation resolved by orchestration.",
            "retryable": False,
            "next_retry_at": None,
            "provider_reference": None,
            "correlation_id": get_correlation_id(request),
            "idempotency_key": payload.idempotency_key,
            "trace_id": get_trace_id(request),
        },
        "adapter_response": adapter_response,
    }


def _persist_conversation_state(
    *,
    request: Request,
    payload: PromptExecutionRequest,
    execution_id: str,
    prompt_checksum: str,
    effective_prompt_text: str,
    raw_prompt_text: str,
    assistant_answer_text: str | None,
    assistant_answer_summary: str | None,
    turn_resolution: Mapping[str, object] | None,
    intent_class: str,
    tax_domain_hint: str,
    selected_route: Mapping[str, object] | None,
    plan: Mapping[str, object],
    supported_lane_id: str | None,
    historical_version_id: str | None,
    regime_identifier: str | None,
    tax_year: int | None,
    mapped_result: Mapping[str, object],
    adapter_response: Mapping[str, object] | None,
    grounded_evidence: Sequence[Mapping[str, object]] | None,
    citations: Sequence[Mapping[str, object]] | None,
    stated_facts: ExtractedTaxpayerFacts,
) -> None:
    store = _get_conversation_state_store(request)
    timed_print("[PERSISTENCE] About to access conversation-state store")
    existing_records = store.list_recent(
        tenant_id=payload.tenant_id,
        conversation_id=payload.conversation_id,
        user_id=payload.effective_taxpayer_user_id,
        limit=50,
    )
    timed_print(
        "[PERSISTENCE] Accessed conversation-state store "
        f"record_count={len(existing_records)}"
    )
    for existing in existing_records:
        if existing["execution_id"] != execution_id:
            continue
        existing_checksum = existing["context_payload"].get("prompt_checksum")
        if existing_checksum == prompt_checksum:
            # AES-GCM intentionally creates a fresh nonce on every encryption.
            # A replay must retain the original ciphertext rather than treating
            # its equivalent plaintext as a conflicting state write.
            return
        raise ConversationStateStoreError(
            reason_code="conversation_state_conflict",
            message="Conversation-state record conflicts with an existing execution context.",
        )
    protected_stated_facts = protect_stated_facts(
        stated_facts=stated_facts,
        protector=_get_conversation_state_protector(request),
    )
    context_payload = build_conversation_state_payload(
        execution_id=execution_id,
        prompt_text=effective_prompt_text,
        prompt_checksum=prompt_checksum,
        intent_class=intent_class,
        tax_domain_hint=tax_domain_hint,
        selected_route=selected_route,
        plan=plan,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        regime_identifier=regime_identifier,
        tax_year=tax_year,
        mapped_result=mapped_result,
        adapter_response=adapter_response,
        grounded_evidence=grounded_evidence,
        citations=citations,
        stated_facts=protected_stated_facts,
    )
    if turn_resolution is not None:
        # Persist only structured, audit-safe resolution fields; never model reasoning.
        for field in (
            "relationship", "operation_mode", "answerability", "retrieval_tax_domain_filter",
            "jurisdiction_hint", "tax_year_hint", "assumptions", "referenced_candidate_ids",
            "clarification_reason_code", "clarification_question", "required_context_fields",
        ):
            context_payload[field] = turn_resolution.get(field)
    if assistant_answer_summary is not None:
        context_payload["assistant_answer_summary"] = assistant_answer_summary
    if assistant_answer_text is not None:
        context_payload["assistant_answer_text"] = assistant_answer_text
    context_payload["raw_prompt_text"] = raw_prompt_text
    context_payload["contextualized_prompt_text"] = effective_prompt_text
    timed_print("[PERSISTENCE] About to persist conversation-state record")
    store.put(
        {
            "execution_id": execution_id,
            "tenant_id": payload.tenant_id,
            "conversation_id": payload.conversation_id,
            "user_id": payload.effective_taxpayer_user_id,
            "context_payload": context_payload,
        }
    )
    timed_print(
        "[PERSISTENCE] Persisted conversation-state record "
        f"execution_id={execution_id}"
    )


def _persist_clarification_turn(
    *, request: Request, payload: PromptIngestionRequest, owner: TrustedConversationOwner, resolution: Mapping[str, object]
) -> None:
    """Persist an explicit assistant clarification so only the next turn can answer it."""
    clarification = cast(Mapping[str, object], resolution.get("clarification") or {})
    state_payload = {
        "execution_id": str(resolution["decision_id"]), "raw_prompt_text": payload.prompt.text,
        "contextualized_prompt_text": cast(Mapping[str, object], resolution.get("turn_resolution") or {}).get("contextualized_prompt"),
        "intent_class": resolution.get("intent_class"), "tax_domain_hint": resolution.get("tax_domain_hint"),
        "assistant_turn_kind": "clarification", "turn_outcome_kind": "clarification_required",
        "clarification_reason_code": clarification.get("reason_code"), "clarification_question": clarification.get("message"),
        "clarification_requested_fields": clarification.get("required_context_fields", []),
    }
    try:
        _get_conversation_state_store(request).put({"execution_id": str(resolution["decision_id"]), "tenant_id": payload.tenant_id, "conversation_id": payload.conversation_id, "user_id": owner["effective_taxpayer_user_id"], "context_payload": state_payload})
    except ConversationStateStoreError:
        # The response remains valid; persistence availability is not a user clarification.
        return


def _persist_failed_turn_outcome(
    *,
    request: Request,
    payload: PromptIngestionRequest,
    user_id: str,
    prompt_text: str,
    prompt_checksum: str,
    error_code: str,
    reason_code: str,
    message: str,
    stage: str,
    intent_class: str,
    tax_domain_hint: str,
) -> None:
    """Persist a bounded failed-turn envelope for safe future explanation."""

    store = _get_conversation_state_store(request)
    failure_execution_id = _compute_prompt_decision_id(
        prompt_checksum=prompt_checksum,
        intent_class="failed_turn",
        plan_id=f"{stage}:{reason_code}",
        tenant_id=payload.tenant_id,
        conversation_id=payload.conversation_id,
    )
    preview = bounded_preview(prompt_text)
    try:
        timed_print("[PERSISTENCE] About to persist failed turn outcome")
        store.put(
            {
                "execution_id": failure_execution_id,
                "tenant_id": payload.tenant_id,
                "conversation_id": payload.conversation_id,
                "user_id": user_id,
                "context_payload": {
                    "conversation_state_schema_version": "2026-07-27",
                    "execution_id": failure_execution_id,
                    "prompt_text": preview,
                    "prompt_preview": preview,
                    "prompt_checksum": prompt_checksum,
                    "intent_class": intent_class,
                    "tax_domain_hint": tax_domain_hint,
                    "turn_outcome_kind": "execution_failure",
                    "error_code": error_code,
                    "reason_code": reason_code,
                    "user_facing_message": message,
                    "failure_summary": message,
                    "retryable": False,
                    "reusable_for_execution": False,
                    "reusable_for_evidence": False,
                    "reusable_for_explanation": True,
                    "stage": stage,
                    "correlation_id": get_correlation_id(request),
                    "trace_id": get_trace_id(request),
                },
            }
        )
        timed_print(
            "[PERSISTENCE] Persisted failed turn outcome "
            f"execution_id={failure_execution_id}"
        )
    except ConversationStateStoreError:
        return None


def _first_available_reference(payload: object) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    normalized = cast(Mapping[str, object], payload)
    for field_name in (
        "artifact_id",
        "form_ready_reference",
        "report_id",
        "document_id",
        "document_reference",
    ):
        value = normalized.get(field_name)
        if isinstance(value, str) and value:
            return value
    return None


def _audit_fact_mismatch_summary(
    mismatches: Sequence[FactMismatch],
) -> list[dict[str, str]]:
    """Project mismatch evidence for audit without recording sensitive values."""

    return [
        {
            "field": mismatch["field"],
            "prior_execution_id": mismatch["prior_execution_id"],
        }
        for mismatch in mismatches
    ]


def _merge_user_fact_integrity_signals(
    *,
    fact_mismatches: Sequence[FactMismatch],
    model_signals: Sequence[FactMismatch | str],
) -> list[FactMismatch | str]:
    """Preserve deterministic mismatches alongside model-declared fact gaps."""

    return [*fact_mismatches, *[signal for signal in model_signals if isinstance(signal, str)]]


def _compute_confidence_flag(
    signals: ResponseIntegritySignals,
) -> Literal["high", "medium", "low"]:
    """Derive the response confidence flag solely from governed integrity signals."""

    if (
        not signals.verification_is_verified
        or signals.synthesis_tool_iterations_used >= MAX_SYNTHESIS_TOOL_ITERATIONS
        or signals.unverified_or_contradicting_user_facts
    ):
        return "low"
    if signals.unsupported_claims or signals.contradictions_found:
        return "medium"
    return "high"


def _post_grounding_missing_fact_fields(
    signals: Sequence[FactMismatch | str],
) -> list[str]:
    """Return bounded absent-fact identifiers emitted by synthesis."""

    allowed_fields = {"income", "turnover", "residency", "filing_status"}
    return [signal for signal in signals if isinstance(signal, str) and signal in allowed_fields]


def _raise_for_post_grounding_fact_clarification(
    *,
    request: Request,
    missing_fact_fields: Sequence[str],
) -> None:
    """Reuse the established clarification envelope for material fact gaps."""

    labels = {
        "income": "income",
        "turnover": "turnover",
        "residency": "residency status",
        "filing_status": "filing status",
    }
    requested = [labels[field] for field in missing_fact_fields]
    joined = ", ".join(requested)
    raise HTTPException(
        status_code=409,
        detail=_error_envelope(
            request=request,
            error_code="clarification_required",
            message=(
                f"Please provide your {joined} before orchestration can give a specific answer."
            ),
            reason="clarification_required",
            reason_code="clarification_required",
            context={
                "required_context_fields": list(missing_fact_fields),
                "candidate_service_families": [],
            },
        ),
    )


_answer_verification_engine = AnswerVerificationEngine()


def _verify_synthesized_answer(
    *,
    answer: UnifiedAnswerResponseModel,
    grounded_evidence: list[dict[str, object]],
    original_prompt: str,
    resolved_tax_domain: str | None = None,
    resolved_entity: str | None = None,
) -> dict[str, object]:
    """Run post-generation verification and return a safe result dict."""
    try:
        try:
            timed_print("[VERIFICATION] About to run answer verification engine")
            result = _answer_verification_engine.verify_answer(
                answer=answer,
                grounded_evidence=grounded_evidence,
                original_prompt=original_prompt,
                resolved_tax_domain=resolved_tax_domain,
                resolved_entity=resolved_entity,
            )
            timed_print(
                "[VERIFICATION] Answer verification engine completed "
                f"is_verified={result['is_verified']}"
            )
        except TypeError as error:
            error_message = str(error)
            if "resolved_tax_domain" not in error_message and "resolved_entity" not in error_message:
                raise
            timed_print(
                "[VERIFICATION] About to run fallback answer verification engine"
            )
            result = _answer_verification_engine.verify_answer(
                answer=answer,
                grounded_evidence=grounded_evidence,
                original_prompt=original_prompt,
            )
            timed_print(
                "[VERIFICATION] Fallback answer verification engine completed "
                f"is_verified={result['is_verified']}"
            )
        return cast(dict[str, object], result)
    except Exception:
        timed_print("[VERIFICATION] Answer verification failed")
        return {
            "is_verified": False,
            "confidence_score": 0.0,
            "issues_found": ["verification_engine_error"],
            "failed_checks": ["verification_engine_error"],
            "verification_type": "composite",
        }


def _extract_grounded_evidence(
    adapter_response: dict[str, object],
) -> list[dict[str, object]] | None:
    result_payload = adapter_response.get("result_payload")
    if not isinstance(result_payload, dict):
        return None
    result_payload_dict = cast(dict[str, object], result_payload)
    grounded_evidence = result_payload_dict.get("grounded_evidence")
    if not isinstance(grounded_evidence, list):
        return None
    normalized: list[dict[str, object]] = []
    for item in cast(list[object], grounded_evidence):
        if not isinstance(item, dict):
            raise ValueError("Grounded evidence payload must contain object items.")
        normalized_item = dict(cast(dict[str, object], item))
        url_value = normalized_item.get("url")
        source_id_value = normalized_item.get("source_id")
        anchor_id_value = normalized_item.get("anchor_id")
        timeline_position = normalized_item.get("timeline_position")
        if (
            "canonical_source_ref" not in normalized_item
            or not isinstance(normalized_item.get("canonical_source_ref"), str)
            or not str(normalized_item.get("canonical_source_ref", "")).strip()
        ):
            if isinstance(url_value, str) and url_value.strip():
                normalized_item["canonical_source_ref"] = url_value
            elif isinstance(anchor_id_value, str) and anchor_id_value.strip():
                normalized_item["canonical_source_ref"] = anchor_id_value
            elif isinstance(source_id_value, str) and source_id_value.strip():
                normalized_item["canonical_source_ref"] = source_id_value
        if normalized_item.get("knowledge_route_mode") not in {
            "search",
            "retrieve",
            "timeline_search",
        }:
            normalized_item["knowledge_route_mode"] = (
                "timeline_search" if isinstance(timeline_position, int) else "search"
            )
        if not isinstance(timeline_position, int):
            normalized_item["timeline_position"] = None
        normalized.append(normalized_item)
    return normalized


def _build_orchestration_governed_validation(
    *,
    selected_route: OrchestrationRouteSelection | None,
    tax_domain_hint: str,
    adapter_response: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if selected_route is None or adapter_response is None:
        return None
    if selected_route.target_service not in {"forms", "reports"}:
        return None
    result_payload = adapter_response.get("result_payload")
    if not isinstance(result_payload, Mapping):
        return None
    envelope = evaluate_orchestration_workflow_validation(
        target_service=selected_route.target_service,
        tax_domain=tax_domain_hint,
        result_payload=cast(Mapping[str, object], result_payload),
    )
    if envelope is None:
        return None
    return envelope.to_dict()


def _build_nonfatal_error(
    *,
    request: Request,
    error_code: str,
    message: str,
    reason_code: str,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    return cast(
        dict[str, object],
        build_orchestration_error_envelope(
            correlation_id=get_correlation_id(request),
            trace_id=get_trace_id(request),
            error_code=error_code,
            message=message,
            reason=reason_code,
            reason_code=reason_code,
            context=context,
        ),
    )


def _fallback_answer_mode(
    *,
    expected_intent_class: str | None,
    selected_route: OrchestrationRouteSelection | None,
) -> AnswerMode:
    if selected_route is not None:
        if selected_route.target_service == "forms":
            return "forms_execution"
        if selected_route.target_service == "reports":
            return "reports_execution"
        if selected_route.target_service == "document_ai":
            return "document_extraction"
        if selected_route.target_service == "knowledge":
            return "grounded_knowledge"
    if expected_intent_class in {
        "lookup_grounded_knowledge",
        "retrieve_grounded_knowledge",
    }:
        return "grounded_knowledge"
    if expected_intent_class == "compute_plus_grounding":
        return "compute_plus_grounding"
    return "compute_execution"


def _error_envelope(
    *,
    request: Request,
    error_code: str,
    message: str,
    reason: str,
    reason_code: str,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    return cast(
        dict[str, object],
        build_orchestration_error_envelope(
            correlation_id=get_correlation_id(request),
            trace_id=get_trace_id(request),
            error_code=error_code,
            message=message,
            reason=reason,
            reason_code=reason_code,
            context=context,
        ),
    )


def _http_error(
    *,
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    reason: str,
    reason_code: str,
    context: dict[str, object] | None = None,
) -> HTTPException:
    emit_income_tax_audit_event(
        event_type="orchestration_request_rejected",
        status="rejected",
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        context={
            "resource_id": request.url.path,
            "path": request.url.path,
            "method": request.method,
            "error_code": error_code,
            "reason": reason,
            "reason_code": reason_code,
            **(context or {}),
        },
    )
    return HTTPException(
        status_code=status_code,
        detail=_error_envelope(
            request=request,
            error_code=error_code,
            message=message,
            reason=reason,
            reason_code=reason_code,
            context=context,
        ),
    )


async def _handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    _ = exc
    try:
        emit_income_tax_audit_event(
            event_type="orchestration_request_rejected",
            status="rejected",
            correlation_id=get_correlation_id(request),
            trace_id=get_trace_id(request),
            context={
                "resource_id": request.url.path,
                "path": request.url.path,
                "method": request.method,
                "error_code": INVALID_ORCHESTRATION_REQUEST,
                "reason": INVALID_ORCHESTRATION_REQUEST,
                "reason_code": INVALID_ORCHESTRATION_REQUEST,
            },
        )
    except OrchestrationAuditStoreError:
        return await _handle_orchestration_audit_store_error(
            request,
            OrchestrationAuditStoreError(
                reason_code="audit_persistence_unavailable",
                message="Orchestration audit persistence is unavailable.",
            ),
        )
    return JSONResponse(
        status_code=400,
        content={
            "detail": _error_envelope(
                request=request,
                error_code=INVALID_ORCHESTRATION_REQUEST,
                message="Orchestration request payload is invalid.",
                reason=INVALID_ORCHESTRATION_REQUEST,
                reason_code=INVALID_ORCHESTRATION_REQUEST,
            )
        },
    )


async def _handle_http_exception_error(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    detail = cast(dict[str, object], exc.detail) if isinstance(exc.detail, dict) else {}
    envelope = _error_envelope(
        request=request,
        error_code=str(detail.get("error_code", INVALID_ORCHESTRATION_REQUEST)),
        message=str(detail.get("message", "Orchestration request failed.")),
        reason=str(detail.get("reason", INVALID_ORCHESTRATION_REQUEST)),
        reason_code=str(
            detail.get(
                "reason_code",
                detail.get("reason", INVALID_ORCHESTRATION_REQUEST),
            )
        ),
        context=cast(dict[str, object] | None, detail.get("context")),
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": envelope})


async def _handle_orchestration_runtime_error(
    request: Request,
    exc: OrchestrationRuntimeError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": _error_envelope(
                request=request,
                error_code=exc.error_code,
                message=exc.message,
                reason=exc.reason,
                reason_code=exc.reason_code,
                context=exc.context,
            )
        },
    )


async def _handle_orchestration_audit_store_error(
    request: Request,
    exc: OrchestrationAuditStoreError,
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": _error_envelope(
                request=request,
                error_code="audit_persistence_failure",
                message=exc.message,
                reason=exc.reason_code,
                reason_code=exc.reason_code,
                context={
                    "resource_id": request.url.path,
                    "path": request.url.path,
                    "method": request.method,
                },
            )
        },
    )


async def _handle_starlette_http_exception_error(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": _error_envelope(
                request=request,
                error_code=UNSUPPORTED_ORCHESTRATION_SCOPE,
                message="Requested orchestration path is not supported.",
                reason=UNSUPPORTED_ORCHESTRATION_SCOPE,
                reason_code=UNSUPPORTED_ORCHESTRATION_SCOPE,
            )
        },
    )


app = create_app()


def _is_high_risk_action_context_unsafe(
    action_context: ActionSafetyContext | None,
) -> bool:
    if action_context is None:
        return False
    if action_context.risk_class != "high":
        return False
    return not (
        action_context.confirmation_state == "confirmed"
        and action_context.step_up_proof_state == "bound"
    )
