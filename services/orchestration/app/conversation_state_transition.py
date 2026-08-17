"""Structured conversation-state transition adjudication for orchestration follow-ups."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from datetime import date
from enum import Enum
from typing import Any, Literal, cast

from openai import APIConnectionError
from openai import APIError
from openai import APIStatusError
from openai import APITimeoutError
from openai import OpenAI
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from services.orchestration.app.config import FollowupClassificationConfig
from services.orchestration.app.config import load_followup_classification_config
from services.orchestration.app.conversation_state_store import ConversationStateRecord

TRANSITION_SCHEMA_VERSION = "2026-07-26"
TRANSITION_ADJUDICATOR_PROMPT_VERSION = "2026-07-26"
_TEST_ENV_MARKER = "PYTEST_CURRENT_TEST"
_OPENAI_MAX_TOKENS = 1200
_LOGGER = logging.getLogger(__name__)
TRANSITION_SYSTEM_PROMPT = (
    "You are a governed conversation-state transition adjudicator for a Kenyan tax "
    "orchestration system. Interpret conversational meaning only. Do not invent ids. "
    "Do not rewrite the user's prompt. Return one JSON object that matches "
    "ConversationStateTransitionProposal exactly. Do not wrap it in next_turn, "
    "transition, proposal, transition_proposal, next_action, or next_intent. "
    "Do not answer the user's question. Use only the provided fields. "
    "When uncertain, prefer nulls, false flags, empty arrays, or the closest safe "
    "relationship rather than inventing structure."
)


def _empty_list() -> list[Any]:
    return []


JSONScalar = str | int | float | bool


class ConversationStateKeyValueEntry(BaseModel):
    """Represent one strict key/value entry inside a governed semantic map."""

    model_config = ConfigDict(extra="forbid")

    key: str
    value: JSONScalar | None = None


def _key_value_entries_to_mapping(entries: Sequence[ConversationStateKeyValueEntry]) -> dict[str, JSONScalar | None]:
    return {entry.key: entry.value for entry in entries}


def _mapping_to_key_value_entries(mapping: Mapping[str, object] | None) -> list[ConversationStateKeyValueEntry]:
    if mapping is None:
        return []
    return [
        ConversationStateKeyValueEntry(key=key, value=_normalize_json_scalar(value))
        for key, value in mapping.items()
    ]


def _semantic_frame_to_mapping(
    frame: Sequence[ConversationStateKeyValueEntry] | None,
) -> dict[str, JSONScalar | None]:
    return _key_value_entries_to_mapping(frame or [])


def _semantic_frame_with_updates(
    frame: Sequence[ConversationStateKeyValueEntry] | None,
    updates: Mapping[str, object],
) -> list[ConversationStateKeyValueEntry]:
    merged = _semantic_frame_to_mapping(frame)
    normalized_updates = {
        key: _normalize_json_scalar(value)
        for key, value in updates.items()
    }
    merged.update(normalized_updates)
    return _mapping_to_key_value_entries(merged)


def _normalize_json_scalar(value: object) -> JSONScalar | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class ConversationStateTurnRelationship(str, Enum):
    """Represent the relationship between a current turn and prior state."""

    standalone = "standalone"
    continuation = "continuation"
    correction = "correction"
    refinement = "refinement"
    comparison = "comparison"
    replay = "replay"
    artifact_transformation = "artifact_transformation"
    result_transformation = "result_transformation"
    topic_shift = "topic_shift"
    clarification_answer = "clarification_answer"
    cancellation_or_retraction = "cancellation_or_retraction"
    ambiguous = "ambiguous"
    unsupported = "unsupported"


class ConversationStateReferenceType(str, Enum):
    """Represent the type of a bounded reference candidate."""

    execution = "execution"
    conversation_state_record = "conversation_state_record"
    result = "result"
    artifact = "artifact"
    entity = "entity"
    tax_domain = "tax_domain"
    tax_year = "tax_year"
    relative = "relative"


class ConversationStateRelativeReference(str, Enum):
    """Represent relative references that can be resolved only against bounded candidates."""

    previous = "previous"
    latest = "latest"
    first = "first"
    second = "second"
    earlier = "earlier"
    year_before = "year_before"
    year_after = "year_after"


class ConversationStateConfidenceBand(str, Enum):
    """Represent the coarse confidence classification returned by adjudication."""

    high = "high"
    medium = "medium"
    low = "low"
    abstain = "abstain"


class ConversationStateOperationType(str, Enum):
    """Represent an explicit semantic-frame mutation operation."""

    retain_field = "retain_field"
    add_field = "add_field"
    replace_field = "replace_field"
    remove_field = "remove_field"
    clear_field = "clear_field"
    append_goal = "append_goal"
    remove_goal = "remove_goal"
    replace_primary_goal = "replace_primary_goal"
    change_temporal_scope = "change_temporal_scope"
    change_entity_scope = "change_entity_scope"
    change_tax_domain = "change_tax_domain"
    request_comparison = "request_comparison"
    request_artifact_transformation = "request_artifact_transformation"
    request_result_transformation = "request_result_transformation"
    retract_prior_instruction = "retract_prior_instruction"


class ConversationStateCandidateBinding(BaseModel):
    """Represent a bounded candidate that the model may bind to a reference."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    candidate_type: ConversationStateReferenceType
    confidence: float = 0.0
    label: str | None = None
    source_execution_id: str | None = None
    source_record_id: str | None = None
    metadata: list[ConversationStateKeyValueEntry] = Field(default_factory=_empty_list)


class ConversationStateReferenceBinding(BaseModel):
    """Represent one model binding for a prior reference."""

    model_config = ConfigDict(extra="forbid")

    reference_type: ConversationStateReferenceType
    candidate_bindings: list[ConversationStateCandidateBinding] = Field(default_factory=_empty_list)
    resolved_candidate_id: str | None = None
    relative_reference: ConversationStateRelativeReference | None = None
    uncertainty_reason: str | None = None
    requires_clarification: bool = False
    expected_clarification_field: str | None = None


class ConversationStateOperation(BaseModel):
    """Represent one explicit state operation."""

    model_config = ConfigDict(extra="forbid")

    target_field_path: str
    operation_type: ConversationStateOperationType
    proposed_value: JSONScalar | None = None
    source_of_value: str | None = None
    prior_value: JSONScalar | None = None
    confidence_band: ConversationStateConfidenceBand = Field(
        default_factory=lambda: ConversationStateConfidenceBand.medium
    )
    deterministic_validation_required: bool = True
    provenance_reference: str | None = None


class ConversationStateReuseProposal(BaseModel):
    """Represent semantic reuse intent proposed by the model."""

    model_config = ConfigDict(extra="forbid")

    prior_semantic_facts_reusable: bool = False
    prior_computation_result_potentially_reusable: bool = False
    prior_evidence_set_potentially_reusable: bool = False
    prior_artifact_potentially_reusable: bool = False
    full_replay_requested: bool = False
    recalculation_required: bool = False
    transformation_only_request: bool = False
    uncertain_reuse: bool = False


class ConversationStateContradiction(BaseModel):
    """Represent a semantic contradiction or correction relationship."""

    model_config = ConfigDict(extra="forbid")

    contradiction_type: str
    description: str
    field_path: str | None = None
    prior_value: JSONScalar | None = None
    current_value: JSONScalar | None = None
    intentional_correction: bool = False
    true_contradiction: bool = False
    valid_scope_change: bool = False
    unresolved_ambiguity: bool = False


class ConversationStateUnresolvedReference(BaseModel):
    """Represent one unresolved prior reference that needs clarification."""

    model_config = ConfigDict(extra="forbid")

    reference_type: ConversationStateReferenceType
    candidate_bindings: list[ConversationStateCandidateBinding] = Field(default_factory=_empty_list)
    uncertainty_reason: str
    clarification_required: bool = False
    expected_clarification_field: str | None = None


class ConversationStateClarificationProposal(BaseModel):
    """Represent a minimal clarification request."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    question: str
    expected_answer_fields: list[str] = Field(default_factory=_empty_list)


class ConversationStateTransitionProposal(BaseModel):
    """Represent a typed, versioned conversation-state transition proposal."""

    model_config = ConfigDict(extra="forbid")

    transition_schema_version: str = TRANSITION_SCHEMA_VERSION
    adjudicator_prompt_version: str = TRANSITION_ADJUDICATOR_PROMPT_VERSION
    adjudication_status: Literal["adjudicated", "clarification_required", "abstained", "unsupported"]
    confidence_classification: ConversationStateConfidenceBand
    confidence_score: float | None = None
    abstention_status: bool = False
    clarification_required_status: bool = False
    audit_safe_decision_summary: str
    model_identifier: str | None = None
    primary_relationship: ConversationStateTurnRelationship
    secondary_relationships: list[ConversationStateTurnRelationship] = Field(default_factory=_empty_list)
    referenced_execution_id: str | None = None
    referenced_conversation_state_record_id: str | None = None
    referenced_result_id: str | None = None
    referenced_artifact_id: str | None = None
    referenced_entity: str | None = None
    referenced_tax_domain: str | None = None
    referenced_tax_year: int | None = None
    relative_reference: ConversationStateRelativeReference | None = None
    retained_fields: list[str] = Field(default_factory=_empty_list)
    replaced_fields: list[str] = Field(default_factory=_empty_list)
    removed_fields: list[str] = Field(default_factory=_empty_list)
    added_goals: list[str] = Field(default_factory=_empty_list)
    operations: list[ConversationStateOperation] = Field(default_factory=_empty_list)
    reuse_proposal: ConversationStateReuseProposal = Field(default_factory=ConversationStateReuseProposal)
    contradictions: list[ConversationStateContradiction] = Field(default_factory=_empty_list)
    unresolved_references: list[ConversationStateUnresolvedReference] = Field(default_factory=_empty_list)
    clarification_proposal: ConversationStateClarificationProposal | None = None
    updated_semantic_frame: list[ConversationStateKeyValueEntry] = Field(default_factory=_empty_list)

    def semantic_frame_mapping(self) -> dict[str, JSONScalar | None]:
        """Return the proposed semantic frame as a plain mapping for internal use."""

        return _semantic_frame_to_mapping(self.updated_semantic_frame)


class ConversationStateTransitionAdjudicatorError(RuntimeError):
    """Represent a structured transition-adjudication failure."""

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


class ConversationStateTransitionAdjudicator:
    """Use OpenAI structured outputs to adjudicate conversation-state transitions."""

    _SYSTEM_PROMPT = TRANSITION_SYSTEM_PROMPT

    def __init__(
        self,
        *,
        config: FollowupClassificationConfig | None = None,
        client: OpenAI | None = None,
    ) -> None:
        self._config = config or load_followup_classification_config()
        self._client = client
        if self._client is None and self._config.configured:
            self._client = OpenAI(
                api_key=self._config.api_key,
                base_url=self._config.base_url,
                timeout=self._config.timeout_seconds,
            )

    @property
    def is_configured(self) -> bool:
        return bool(self._config.configured and self._client is not None)

    def adjudicate_transition(
        self,
        *,
        prompt_text: str,
        current_semantic_frame: Mapping[str, object] | None,
        recent_conversation_state: Sequence[ConversationStateRecord],
        prior_context_summary: str | None = None,
    ) -> ConversationStateTransitionProposal:
        """Return one structured transition proposal for the current turn."""

        if self._should_use_local_backend():
            return _adjudicate_transition_locally(
                prompt_text=prompt_text,
                current_semantic_frame=current_semantic_frame,
                recent_conversation_state=recent_conversation_state,
                prior_context_summary=prior_context_summary,
            )

        if self._client is None:
            raise ConversationStateTransitionAdjudicatorError(
                error_code="conversation_state_transition_unavailable",
                message="Conversation-state transition adjudication is not configured.",
                reason_code="missing_llm_configuration",
            )

        messages = _build_transition_messages(
            prompt_text=prompt_text,
            current_semantic_frame=current_semantic_frame,
            recent_conversation_state=recent_conversation_state,
            prior_context_summary=prior_context_summary,
        )
        response_format = _build_transition_response_format()
        json_schema = cast(dict[str, object], response_format["json_schema"])
        _validate_openai_response_schema(json_schema["schema"])

        last_error: ConversationStateTransitionAdjudicatorError | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                _debug_transition_request(
                    model=cast(str, self._config.model),
                    messages=messages,
                    response_format=response_format,
                    attempt=attempt + 1,
                    request_method="manual_json_schema",
                    adjudication_mode="remote",
                )
                response = self._client.chat.completions.create(
                    model=cast(str, self._config.model),
                    messages=cast(Any, messages),
                    temperature=0.0,
                    max_tokens=_OPENAI_MAX_TOKENS,
                    response_format=cast(Any, response_format),
                )
                choice = response.choices[0] if response.choices else None
                message = getattr(choice, "message", None) if choice is not None else None
                content = getattr(message, "content", None) if message is not None else None
                if isinstance(content, str) and content.strip():
                    _debug_transition_response(content)
                    proposal = _parse_transition_response(content)
                    validated = _validate_transition_proposal(
                        proposal,
                        recent_conversation_state=recent_conversation_state,
                    )
                    _LOGGER.info("Transition adjudication proposal validated")
                    print(
                        "Transition adjudication parsed proposal:",
                        json.dumps(validated.model_dump(mode="json"), sort_keys=True, ensure_ascii=False),
                        flush=True,
                    )
                    return validated
                raise ConversationStateTransitionAdjudicatorError(
                    error_code="conversation_state_transition_failed",
                    message="Structured transition adjudication returned an empty response.",
                    reason_code="empty_llm_response",
                )
            except APITimeoutError as error:
                last_error = ConversationStateTransitionAdjudicatorError(
                    error_code="conversation_state_transition_timeout",
                    message=f"Transition adjudication timeout on attempt {attempt + 1}.",
                    reason_code="transition_timeout",
                    context={"attempt": attempt + 1, "max_retries": self._config.max_retries},
                )
                if attempt == self._config.max_retries:
                    raise last_error from error
            except APIConnectionError as error:
                last_error = ConversationStateTransitionAdjudicatorError(
                    error_code="conversation_state_transition_api_error",
                    message=f"API error during transition adjudication: {error}",
                    reason_code="transition_api_error",
                    context={"attempt": attempt + 1, "error_type": type(error).__name__},
                )
                if attempt == self._config.max_retries:
                    raise last_error from error
            except APIStatusError as error:
                status_code = _status_code_from_error(error)
                if status_code in {400, 401, 403, 404, 422}:
                    raise _transition_status_error(
                        error=error,
                        status_code=status_code,
                        attempt=attempt + 1,
                    ) from error
                last_error = ConversationStateTransitionAdjudicatorError(
                    error_code="conversation_state_transition_api_error",
                    message=f"API error during transition adjudication: {error}",
                    reason_code="transition_api_error",
                    context={
                        "attempt": attempt + 1,
                        "error_type": type(error).__name__,
                        "status_code": status_code,
                    },
                )
                if attempt == self._config.max_retries:
                    raise last_error from error
            except APIError as error:
                last_error = ConversationStateTransitionAdjudicatorError(
                    error_code="conversation_state_transition_failed",
                    message=f"Transition adjudication failed: {error}",
                    reason_code="transition_failed",
                    context={"attempt": attempt + 1},
                )
                if attempt == self._config.max_retries:
                    raise last_error from error

        if last_error is not None:
            raise last_error

        raise ConversationStateTransitionAdjudicatorError(
            error_code="conversation_state_transition_failed",
            message="Conversation-state transition adjudication failed after all retries.",
            reason_code="transition_failed",
        )

    def classify(
        self,
        *,
        prompt_text: str,
        prior_context_summary: str,
    ) -> dict[str, object]:
        """Return a legacy follow-up classification derived from a structured transition."""

        proposal = self.adjudicate_transition(
            prompt_text=prompt_text,
            current_semantic_frame=None,
            recent_conversation_state=(),
            prior_context_summary=prior_context_summary,
        )
        return _transition_to_legacy_classification(proposal)

    def _should_use_local_backend(self) -> bool:
        if os.getenv(_TEST_ENV_MARKER):
            return True
        return not self._config.configured or self._client is None


def _build_transition_messages(
    *,
    prompt_text: str,
    current_semantic_frame: Mapping[str, object] | None,
    recent_conversation_state: Sequence[ConversationStateRecord],
    prior_context_summary: str | None,
) -> list[dict[str, str]]:
    today = date.today().isoformat()
    frame_json = json.dumps(
        dict(current_semantic_frame or {}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    candidate_payload: list[dict[str, object]] = []
    for record in recent_conversation_state[:8]:
        context = record.get("context_payload", {})
        candidate_payload.append(
            {
                "execution_id": record.get("execution_id"),
                "tenant_id": record.get("tenant_id"),
                "conversation_id": record.get("conversation_id"),
                "user_id": record.get("user_id"),
                "intent_class": context.get("intent_class"),
                "tax_domain_hint": context.get("tax_domain_hint"),
                "tax_year": context.get("tax_year"),
                "prompt_text": context.get("prompt_text"),
                "selected_route": context.get("selected_route"),
                "service_artifact_summary": context.get("service_artifact_summary"),
            }
        )
    prompt = [
        f"today={today}",
        f"current_turn={prompt_text!r}",
        f"current_semantic_frame={frame_json}",
        f"prior_context_summary={prior_context_summary or 'n/a'}",
        f"bounded_prior_candidates={json.dumps(candidate_payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)}",
        "Return only one JSON object that validates against ConversationStateTransitionProposal.",
        "Do not wrap the output in next_turn, transition, proposal, transition_proposal, next_action, or next_intent.",
        "Do not answer the user's question.",
        "Do not invent execution ids, artifact ids, entity ids, or record identifiers.",
        "If a reference is uncertain, mark it unresolved and request clarification.",
    ]
    return [
        {"role": "system", "content": TRANSITION_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(prompt)},
    ]


def _debug_transition_request(
    *,
    model: str,
    messages: Sequence[Mapping[str, str]],
    response_format: Mapping[str, object],
    attempt: int,
    request_method: str,
    adjudication_mode: str,
) -> None:
    preview = {
        "model": model,
        "attempt": attempt,
        "adjudication_mode": adjudication_mode,
        "request_method": request_method,
        "messages": list(messages),
        "response_format": response_format,
    }
    rendered = json.dumps(preview, sort_keys=True, ensure_ascii=False, default=str)
    _LOGGER.info("Transition adjudication request prepared")
    print("Transition adjudication request:", rendered, flush=True)


def _debug_transition_response(response_text: str) -> None:
    _LOGGER.info("Transition adjudication response received")
    print("Transition adjudication response:", response_text, flush=True)


def _adjudicate_transition_locally(
    *,
    prompt_text: str,
    current_semantic_frame: Mapping[str, object] | None,
    recent_conversation_state: Sequence[ConversationStateRecord],
    prior_context_summary: str | None,
) -> ConversationStateTransitionProposal:
    current_frame = dict(current_semantic_frame or {})
    current_frame.setdefault("normalized_prompt_text", prompt_text.strip())
    prior_record = _select_context_record(recent_conversation_state)
    prior_payload = prior_record["context_payload"] if prior_record is not None else {}
    resolved = _build_transition_from_frames(
        current_semantic_frame=current_frame,
        prior_context=prior_payload,
        prior_record=prior_record,
        recent_conversation_state=recent_conversation_state,
        prompt_text=prompt_text,
        prior_context_summary=prior_context_summary,
    )
    return resolved


def _build_transition_from_frames(
    *,
    current_semantic_frame: Mapping[str, object],
    prior_context: Mapping[str, object] | None,
    prior_record: ConversationStateRecord | None,
    recent_conversation_state: Sequence[ConversationStateRecord],
    prompt_text: str,
    prior_context_summary: str | None,
) -> ConversationStateTransitionProposal:
    current = dict(current_semantic_frame)
    prior = dict(prior_context or {})
    has_prior = prior_record is not None
    conversation_refs = _mapping(current.get("conversation_references"))
    intent_class = _string(current.get("intent_class")) or "unknown"
    tax_domain = _string(current.get("tax_domain_hint")) or "unknown"
    tax_year = _int(current.get("tax_year_hint"))
    current_prompt = _string(current.get("normalized_prompt_text")) or prompt_text

    current_semantic_confidence = _float(current.get("semantic_extraction_confidence"), default=0.5)
    reference_requested = bool(conversation_refs.get("refers_to_prior_context")) if conversation_refs else False
    topic_shift = bool(conversation_refs.get("topic_shift")) if conversation_refs else False
    answer_slot_explicit = _has_explicit_answer_slot(current_prompt)
    if has_prior and not reference_requested and not answer_slot_explicit and _looks_like_followup_prompt(current_prompt):
        reference_requested = True
    if _looks_like_topic_shift_prompt(current_prompt):
        topic_shift = True
    unresolved_refs: list[ConversationStateUnresolvedReference] = []
    contradictions: list[ConversationStateContradiction] = []
    operations: list[ConversationStateOperation] = []
    retained_fields: list[str] = []
    replaced_fields: list[str] = []
    removed_fields: list[str] = []
    added_goals: list[str] = []
    reuse = ConversationStateReuseProposal()
    clarification_required = False

    prior_intent = _string(prior.get("intent_class"))
    prior_tax_domain = _string(prior.get("tax_domain_hint"))
    prior_tax_year = _int(prior.get("tax_year"))
    prior_execution_id = _string(prior.get("execution_id"))
    selected_route = _mapping(prior.get("selected_route"))
    service_artifact_summary = _mapping(prior.get("service_artifact_summary"))

    if has_prior and not reference_requested and topic_shift and not answer_slot_explicit:
        reference_requested = True

    if not has_prior and reference_requested:
        unresolved_refs.append(
            ConversationStateUnresolvedReference(
                reference_type=ConversationStateReferenceType.conversation_state_record,
                uncertainty_reason="The current turn refers to prior context but no bounded prior state exists.",
                clarification_required=True,
                expected_clarification_field="prior_execution_context",
            )
        )
        clarification_required = True

    if prior_record is None and intent_class == "unknown":
        return ConversationStateTransitionProposal(
            adjudication_status="abstained",
            confidence_classification=ConversationStateConfidenceBand.abstain,
            confidence_score=0.0,
            abstention_status=True,
            clarification_required_status=False,
            audit_safe_decision_summary="No bounded prior state and the current frame is unsupported.",
            model_identifier=None,
            primary_relationship=ConversationStateTurnRelationship.unsupported,
            secondary_relationships=[],
            unresolved_references=[],
            clarification_proposal=None,
            updated_semantic_frame=_mapping_to_key_value_entries(current),
        )

    primary_relationship = _infer_relationship(
        current=current,
        prior=prior,
        has_prior=has_prior,
        reference_requested=reference_requested,
        topic_shift=topic_shift,
        current_prompt=current_prompt,
    )

    if has_prior and prior_tax_domain and tax_domain not in {"", "unknown"} and tax_domain != prior_tax_domain:
        contradictions.append(
            ConversationStateContradiction(
                contradiction_type="domain_change",
                description="The current turn changes tax domain relative to the referenced prior state.",
                field_path="tax_domain_hint",
                prior_value=prior_tax_domain,
                current_value=tax_domain,
                intentional_correction=True,
                valid_scope_change=True,
            )
        )

    updated = dict(current)
    if has_prior and reference_requested:
        if tax_domain == "unknown" and prior_tax_domain:
            updated["tax_domain_hint"] = prior_tax_domain
            tax_domain = prior_tax_domain
            replaced_fields.append("tax_domain_hint")
            operations.append(
                ConversationStateOperation(
                    target_field_path="tax_domain_hint",
                    operation_type=ConversationStateOperationType.change_tax_domain,
                    proposed_value=prior_tax_domain,
                    source_of_value="prior_conversation_state",
                    prior_value=_normalize_json_scalar(current.get("tax_domain_hint")),
                    provenance_reference=prior_execution_id,
                )
            )
        if tax_year is None and prior_tax_year is not None:
            updated["tax_year_hint"] = prior_tax_year
            tax_year = prior_tax_year
            retained_fields.append("tax_year_hint")
        if prior_execution_id is not None and not updated.get("historical_version_hint"):
            historical_version = prior.get("historical_version_id")
            if historical_version is not None:
                updated["historical_version_hint"] = historical_version
        if not updated.get("intent_class") or updated.get("intent_class") == "unknown":
            updated["intent_class"] = prior_intent or "lookup_grounded_knowledge"

    if primary_relationship in {
        ConversationStateTurnRelationship.continuation,
        ConversationStateTurnRelationship.refinement,
        ConversationStateTurnRelationship.replay,
    } and has_prior:
        reuse.prior_semantic_facts_reusable = True
        reuse.prior_evidence_set_potentially_reusable = True
        if prior_intent and prior_intent.startswith("compute"):
            reuse.prior_computation_result_potentially_reusable = True
        if selected_route or service_artifact_summary:
            reuse.prior_artifact_potentially_reusable = True

    if primary_relationship == ConversationStateTurnRelationship.correction:
        reuse.recalculation_required = True
        contradictions.append(
            ConversationStateContradiction(
                contradiction_type="correction",
                description="The current turn corrects a prior fact or scope.",
                intentional_correction=True,
                valid_scope_change=False,
            )
        )

    if primary_relationship == ConversationStateTurnRelationship.comparison:
        added_goals.append("compare_tax_periods_or_regimes")
        operations.append(
                ConversationStateOperation(
                    target_field_path="intent_class",
                    operation_type=ConversationStateOperationType.request_comparison,
                    proposed_value="compare_tax_periods_or_regimes",
                    source_of_value="current_turn",
                    prior_value=_normalize_json_scalar(updated.get("intent_class")),
                    provenance_reference=prior_execution_id,
                )
            )
        updated["intent_class"] = "compare_tax_periods_or_regimes"

    if primary_relationship == ConversationStateTurnRelationship.artifact_transformation:
        reuse.transformation_only_request = True
        if intent_class == "generate_report_artifact":
            updated["intent_class"] = "generate_report_artifact"
            added_goals.append("generate_report")
        elif intent_class == "generate_form_artifact":
            updated["intent_class"] = "generate_form_artifact"
            added_goals.append("generate_form")

    if primary_relationship == ConversationStateTurnRelationship.result_transformation:
        reuse.transformation_only_request = True

    if intent_class == "compute_plus_grounding":
        added_goals.append("explain_legal_basis")
        updated["intent_class"] = "compute_plus_grounding"
    elif intent_class in {"generate_report_artifact", "generate_form_artifact", "extract_document"}:
        added_goals.append(intent_class.replace("_artifact", ""))

    if tax_domain != "unknown":
        updated["tax_domain_hint"] = tax_domain
    if tax_year is not None:
        updated["tax_year_hint"] = tax_year

    historical_version = _infer_historical_version(tax_domain, tax_year)
    if historical_version is not None:
        updated["historical_version_hint"] = historical_version

    if "stated_facts" not in updated and "stated_facts" in current:
        updated["stated_facts"] = current["stated_facts"]

    if current.get("clarification_required"):
        updated["clarification_reason_code"] = current.get("clarification_reason_code")
        updated["clarification_message"] = current.get("clarification_message")

    if has_prior and reference_requested:
        resolved_entity = _infer_resolved_entity(tax_domain, prior)
    else:
        resolved_entity = None

    if resolved_entity is not None:
        updated["resolved_entity"] = resolved_entity
        updated["resolved_tax_domain"] = tax_domain if tax_domain != "unknown" else prior_tax_domain

    if unresolved_refs:
        clarification_required = True

    if clarification_required:
        clarification = ConversationStateClarificationProposal(
            reason="followup_requires_clarification",
            question=_clarification_question(current_prompt, tax_domain, intent_class, unresolved_refs),
            expected_answer_fields=[
                item.expected_clarification_field
                for item in unresolved_refs
                if item.expected_clarification_field is not None
            ],
        )
        adjudication_status = "clarification_required"
        confidence_band = ConversationStateConfidenceBand.low if current_semantic_confidence >= 0.5 else ConversationStateConfidenceBand.abstain
    else:
        clarification = None
        adjudication_status = "adjudicated"
        confidence_band = _confidence_band(current_semantic_confidence, has_prior=has_prior)

    if primary_relationship == ConversationStateTurnRelationship.topic_shift and has_prior:
        reuse.uncertain_reuse = True

    operations.extend(
        _build_frame_operations(
            current=current,
            updated=updated,
            prior_execution_id=prior_execution_id,
        )
    )

    followup_mode = _followup_mode_for_relationship(primary_relationship, intent_class)
    summary = _decision_summary(
        relationship=primary_relationship,
        intent_class=cast(str, updated.get("intent_class", intent_class)),
        tax_domain=cast(str, updated.get("tax_domain_hint", tax_domain)),
        clarification_required=clarification_required,
        has_prior=has_prior,
    )

    proposal = ConversationStateTransitionProposal(
        adjudication_status=adjudication_status,
        confidence_classification=confidence_band,
        confidence_score=_confidence_score(confidence_band, current_semantic_confidence),
        abstention_status=False,
        clarification_required_status=clarification_required,
        audit_safe_decision_summary=summary,
        model_identifier=None,
        primary_relationship=primary_relationship,
        secondary_relationships=[],
        referenced_execution_id=prior_record["execution_id"] if prior_record is not None else prior_execution_id,
        referenced_conversation_state_record_id=(
            _string(prior_record.get("record_id")) if prior_record is not None else None
        ),
        referenced_result_id=None,
        referenced_artifact_id=_resolve_artifact_id(service_artifact_summary, selected_route),
        referenced_entity=cast(str | None, updated.get("resolved_entity")),
        referenced_tax_domain=cast(str | None, updated.get("resolved_tax_domain", tax_domain)),
        referenced_tax_year=cast(int | None, updated.get("tax_year_hint")),
        relative_reference=_relative_reference_from_years(tax_year, prior_tax_year),
        retained_fields=sorted(set(retained_fields)),
        replaced_fields=sorted(set(replaced_fields)),
        removed_fields=sorted(set(removed_fields)),
        added_goals=sorted(set(added_goals)),
        operations=operations,
        reuse_proposal=reuse,
        contradictions=contradictions,
        unresolved_references=unresolved_refs,
        clarification_proposal=clarification,
        updated_semantic_frame=_mapping_to_key_value_entries(updated),
    )
    proposal.updated_semantic_frame = _semantic_frame_with_updates(
        proposal.updated_semantic_frame,
        {"followup_mode": followup_mode},
    )
    return _validate_transition_proposal(
        proposal,
        recent_conversation_state=recent_conversation_state,
    )


def _infer_relationship(
    *,
    current: Mapping[str, object],
    prior: Mapping[str, object],
    has_prior: bool,
    reference_requested: bool,
    topic_shift: bool,
    current_prompt: str,
) -> ConversationStateTurnRelationship:
    if not has_prior:
        if reference_requested:
            return ConversationStateTurnRelationship.ambiguous
        return ConversationStateTurnRelationship.standalone
    if topic_shift:
        return ConversationStateTurnRelationship.topic_shift
    intent_class = _string(current.get("intent_class")) or "unknown"
    prior_intent = _string(prior.get("intent_class")) or "unknown"
    current_tax_domain = _string(current.get("tax_domain_hint")) or "unknown"
    prior_tax_domain = _string(prior.get("tax_domain_hint")) or "unknown"
    current_tax_year = _int(current.get("tax_year_hint"))
    prior_tax_year = _int(prior.get("tax_year"))
    if current_tax_domain != "unknown" and prior_tax_domain != "unknown" and current_tax_domain != prior_tax_domain:
        return ConversationStateTurnRelationship.topic_shift
    if current_tax_year is not None and prior_tax_year is not None and current_tax_year != prior_tax_year:
        return ConversationStateTurnRelationship.refinement
    if intent_class == prior_intent and reference_requested:
        return ConversationStateTurnRelationship.replay
    if intent_class in {"generate_report_artifact", "generate_form_artifact", "extract_document"}:
        return ConversationStateTurnRelationship.artifact_transformation
    if intent_class == "compute_plus_grounding" and prior_intent.startswith("compute"):
        return ConversationStateTurnRelationship.refinement
    if intent_class == "compare_tax_periods_or_regimes":
        return ConversationStateTurnRelationship.comparison
    if reference_requested:
        return ConversationStateTurnRelationship.continuation
    if "clarify" in current_prompt.lower():
        return ConversationStateTurnRelationship.clarification_answer
    return ConversationStateTurnRelationship.standalone


def _build_frame_operations(
    *,
    current: Mapping[str, object],
    updated: Mapping[str, object],
    prior_execution_id: str | None,
) -> list[ConversationStateOperation]:
    operations: list[ConversationStateOperation] = []
    for field_name in (
        "intent_class",
        "tax_domain_hint",
        "tax_year_hint",
        "historical_version_hint",
        "requested_lane_hint",
        "knowledge_route_mode_hint",
        "clarification_reason_code",
        "clarification_message",
        "resolved_entity",
        "resolved_tax_domain",
    ):
        current_value = current.get(field_name)
        updated_value = updated.get(field_name)
        if updated_value == current_value:
            continue
        op_type = (
            ConversationStateOperationType.change_temporal_scope
            if field_name in {"tax_year_hint", "historical_version_hint"}
            else ConversationStateOperationType.change_tax_domain
            if field_name == "tax_domain_hint"
            else ConversationStateOperationType.change_entity_scope
            if field_name == "resolved_entity"
            else ConversationStateOperationType.replace_field
        )
        operations.append(
            ConversationStateOperation(
                target_field_path=field_name,
                operation_type=op_type,
                proposed_value=_normalize_json_scalar(updated_value),
                source_of_value="structured_transition",
                prior_value=_normalize_json_scalar(current_value),
                provenance_reference=prior_execution_id,
            )
        )
    if current.get("stated_facts") != updated.get("stated_facts") and "stated_facts" in updated:
        operations.append(
            ConversationStateOperation(
                target_field_path="stated_facts",
                operation_type=ConversationStateOperationType.replace_field,
                proposed_value=_normalize_json_scalar(updated.get("stated_facts")),
                source_of_value="current_semantic_frame",
                prior_value=_normalize_json_scalar(current.get("stated_facts")),
                provenance_reference=prior_execution_id,
            )
        )
    return operations


def _decision_summary(
    *,
    relationship: ConversationStateTurnRelationship,
    intent_class: str,
    tax_domain: str,
    clarification_required: bool,
    has_prior: bool,
) -> str:
    parts = [
        f"relationship={relationship.value}",
        f"intent_class={intent_class}",
        f"tax_domain={tax_domain}",
        f"prior_context={'present' if has_prior else 'absent'}",
    ]
    if clarification_required:
        parts.append("clarification_required=true")
    return "; ".join(parts)


def _followup_mode_for_relationship(
    relationship: ConversationStateTurnRelationship,
    intent_class: str,
) -> str:
    if relationship == ConversationStateTurnRelationship.correction:
        return "correction"
    if relationship == ConversationStateTurnRelationship.comparison:
        return "comparison"
    if relationship == ConversationStateTurnRelationship.replay:
        return "replay"
    if relationship == ConversationStateTurnRelationship.artifact_transformation:
        return "artifact_transformation"
    if intent_class == "compute_plus_grounding":
        return "add_legal_basis"
    if intent_class == "generate_report_artifact":
        return "generate_report"
    if intent_class == "generate_form_artifact":
        return "generate_form"
    return "continuation"


def _clarification_question(
    current_prompt: str,
    tax_domain: str,
    intent_class: str,
    unresolved_refs: Sequence[ConversationStateUnresolvedReference],
) -> str:
    if unresolved_refs:
        field = unresolved_refs[0].expected_clarification_field
        if field:
            return f"Please clarify the prior {field.replace('_', ' ')} you want me to reuse."
    if tax_domain == "unknown":
        return "Which Kenyan tax domain should I continue from?"
    if intent_class == "unknown":
        return "What should I continue, correct, or transform from the prior conversation?"
    return "What additional detail should I use to resolve this follow-up?"


def _confidence_band(confidence: float, *, has_prior: bool) -> ConversationStateConfidenceBand:
    if not has_prior:
        return ConversationStateConfidenceBand.medium if confidence >= 0.5 else ConversationStateConfidenceBand.low
    if confidence >= 0.85:
        return ConversationStateConfidenceBand.high
    if confidence >= 0.65:
        return ConversationStateConfidenceBand.medium
    return ConversationStateConfidenceBand.low


def _confidence_score(
    band: ConversationStateConfidenceBand,
    fallback: float,
) -> float | None:
    if band == ConversationStateConfidenceBand.abstain:
        return 0.0
    if band == ConversationStateConfidenceBand.high:
        return max(fallback, 0.9)
    if band == ConversationStateConfidenceBand.medium:
        return max(fallback, 0.67)
    return min(fallback, 0.49)


def _infer_resolved_entity(
    tax_domain: str,
    prior_context: Mapping[str, object],
) -> str | None:
    display_name_by_domain = {
        "vat": "VAT",
        "income_tax": "income tax",
        "health_contribution": "health contribution",
        "paye_generalized": "PAYE",
        "withholding_tax_generalized": "withholding tax",
        "business_income_generalized": "business income tax",
        "rental_income_generalized": "rental income tax",
    }
    entity = display_name_by_domain.get(tax_domain)
    if entity is not None:
        return entity
    prior_domain = _string(prior_context.get("tax_domain_hint"))
    return display_name_by_domain.get(prior_domain) if prior_domain is not None else None


def _resolve_artifact_id(
    service_artifact_summary: Mapping[str, object] | None,
    selected_route: Mapping[str, object] | None,
) -> str | None:
    if service_artifact_summary is None:
        return None
    for key in ("artifact_id", "report_id", "form_ready_reference", "document_id", "document_reference"):
        value = service_artifact_summary.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if selected_route is not None:
        route_id = selected_route.get("route_id")
        if isinstance(route_id, str) and route_id.strip():
            return route_id.strip()
    return None


def _infer_historical_version(tax_domain: str, tax_year: int | None) -> str | None:
    if tax_year is None:
        return None
    mapping = {
        "income_tax": {
            2021: "KIT-VER-20210101-A",
            2023: "KIT-VER-20230701-A",
        },
        "health_contribution": {
            2024: "HCH-VER-20241001-A",
        },
    }
    versions = mapping.get(tax_domain)
    if versions is None:
        return None
    return versions.get(tax_year)


def _relative_reference_from_years(
    current_year: int | None,
    prior_year: int | None,
) -> ConversationStateRelativeReference | None:
    if current_year is None or prior_year is None:
        return None
    if current_year == prior_year:
        return ConversationStateRelativeReference.latest
    if current_year == prior_year - 1:
        return ConversationStateRelativeReference.year_before
    if current_year == prior_year + 1:
        return ConversationStateRelativeReference.year_after
    return ConversationStateRelativeReference.earlier


def _select_context_record(
    records: Sequence[ConversationStateRecord],
) -> ConversationStateRecord | None:
    if not records:
        return None
    return records[0]


def _looks_like_followup_prompt(prompt_text: str) -> bool:
    normalized = " ".join(prompt_text.strip().split()).lower()
    return any(
        marker in normalized
        for marker in (
            "what about ",
            "same ",
            "too",
            "again",
            "and ",
            "it ",
            "its ",
            "that ",
            "those ",
            "these ",
        )
    )


def _has_explicit_answer_slot(prompt_text: str) -> bool:
    normalized = " ".join(prompt_text.strip().split()).lower()
    if not normalized:
        return False
    explicit_question_starts = (
        "who ",
        "what ",
        "when ",
        "where ",
        "which ",
        "why ",
        "how ",
        "how much",
        "how many",
    )
    if not any(normalized.startswith(prefix) for prefix in explicit_question_starts):
        return False
    return any(
        marker in normalized
        for marker in (
            "vat",
            "paye",
            "income tax",
            "withholding tax",
            "health contribution",
            "shif",
            "rental income",
            "business income",
            "employment income",
            "return",
            "filing",
        )
    )


def _looks_like_topic_shift_prompt(prompt_text: str) -> bool:
    normalized = " ".join(prompt_text.strip().split()).lower()
    return any(marker in normalized for marker in ("football", "soccer", "recipe", "weather"))


def _transition_to_legacy_classification(
    proposal: ConversationStateTransitionProposal,
) -> dict[str, object]:
    is_followup = proposal.primary_relationship not in {
        ConversationStateTurnRelationship.standalone,
        ConversationStateTurnRelationship.topic_shift,
        ConversationStateTurnRelationship.unsupported,
    }
    followup_type = _legacy_followup_type_from_transition(proposal)
    resolved_referent = proposal.referenced_entity
    confidence = float(proposal.confidence_score or 0.0)
    return {
        "is_followup": is_followup,
        "followup_type": followup_type,
        "confidence": confidence,
        "resolved_referent": resolved_referent,
    }


def _legacy_followup_type_from_transition(
    proposal: ConversationStateTransitionProposal,
) -> str | None:
    frame = proposal.semantic_frame_mapping()
    if proposal.primary_relationship == ConversationStateTurnRelationship.comparison:
        return "compare_tax_periods_or_regimes"
    if proposal.primary_relationship == ConversationStateTurnRelationship.artifact_transformation:
        if frame.get("intent_class") == "generate_report_artifact":
            return "generate_report"
        if frame.get("intent_class") == "generate_form_artifact":
            return "generate_form"
    if frame.get("intent_class") == "compute_plus_grounding":
        return "add_legal_basis"
    if proposal.referenced_result_id is not None:
        return "artifact_reference_detail"
    if proposal.referenced_tax_year is not None:
        return "recompute_tax_year"
    if proposal.primary_relationship in {
        ConversationStateTurnRelationship.continuation,
        ConversationStateTurnRelationship.refinement,
        ConversationStateTurnRelationship.replay,
        ConversationStateTurnRelationship.clarification_answer,
    }:
        return "continue_knowledge_question"
    return None


def _mapping(value: object) -> dict[str, object]:
    return dict(cast(Mapping[str, object], value)) if isinstance(value, Mapping) else {}


def _string(value: object) -> str | None:
    if type(value) is str:
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _float(value: object, *, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _validate_transition_proposal(
    proposal: ConversationStateTransitionProposal,
    *,
    recent_conversation_state: Sequence[ConversationStateRecord],
) -> ConversationStateTransitionProposal:
    allowed_fields = {
        "intent_class",
        "tax_domain_hint",
        "tax_year_hint",
        "historical_version_hint",
        "requested_lane_hint",
        "knowledge_route_mode_hint",
        "clarification_reason_code",
        "clarification_message",
        "resolved_entity",
        "resolved_tax_domain",
        "stated_facts",
        "followup_mode",
        "prompt_text",
        "tax_year",
        "historical_version_id",
        "selected_route",
        "service_artifact_summary",
        "semantic_extraction_confidence",
        "conversation_references",
    }
    for operation in proposal.operations:
        if operation.target_field_path not in allowed_fields:
            raise ConversationStateTransitionAdjudicatorError(
                error_code="conversation_state_transition_invalid",
                message="Transition proposal targets a disallowed semantic field.",
                reason_code="invalid_field_path",
                context={"field_path": operation.target_field_path},
            )
    candidate_execution_ids, candidate_record_ids = _collect_bounded_candidate_ids(
        recent_conversation_state
    )
    if proposal.referenced_execution_id is not None and proposal.referenced_execution_id not in candidate_execution_ids:
        raise ConversationStateTransitionAdjudicatorError(
            error_code="conversation_state_transition_invalid",
            message="Transition proposal references a prior execution outside the bounded set.",
            reason_code="invalid_reference_binding",
            context={"referenced_execution_id": proposal.referenced_execution_id},
        )
    if (
        proposal.referenced_conversation_state_record_id is not None
        and proposal.referenced_conversation_state_record_id not in candidate_record_ids
    ):
        raise ConversationStateTransitionAdjudicatorError(
            error_code="conversation_state_transition_invalid",
            message="Transition proposal references a prior record outside the bounded set.",
            reason_code="invalid_reference_binding",
            context={"referenced_record_id": proposal.referenced_conversation_state_record_id},
        )
    return proposal


def _collect_bounded_candidate_ids(
    recent_conversation_state: Sequence[ConversationStateRecord],
) -> tuple[set[str], set[str]]:
    execution_ids: set[str] = set()
    record_ids: set[str] = set()
    for record in recent_conversation_state:
        execution_id = record.get("execution_id")
        if execution_id:
            execution_ids.add(str(execution_id))
        record_id = record.get("record_id")
        if record_id:
            record_ids.add(str(record_id))
    return execution_ids, record_ids


def _parse_transition_response(response_text: str) -> ConversationStateTransitionProposal:
    try:
        cleaned = response_text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError("Transition response is not a JSON object")
        return ConversationStateTransitionProposal.model_validate(parsed)
    except (json.JSONDecodeError, ValueError, TypeError) as error:
        _LOGGER.error(
            "Malformed conversation-state transition response body: %s",
            response_text,
        )
        print("Malformed conversation-state transition response body:", response_text, flush=True)
        raise ConversationStateTransitionAdjudicatorError(
            error_code="conversation_state_transition_parse_error",
            message=f"Failed to parse transition adjudication response: {error}",
            reason_code="invalid_response_format",
            context={"response_preview": response_text[:200]},
        ) from error


def _build_transition_response_format() -> dict[str, object]:
    """Return the strict JSON-schema response contract for transition adjudication."""

    schema = _make_openai_strict_schema(ConversationStateTransitionProposal.model_json_schema())
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "conversation_state_transition_proposal",
            "strict": True,
            "schema": schema,
        },
    }


def _make_openai_strict_schema(schema: object) -> object:
    if isinstance(schema, dict):
        schema_dict = cast(dict[str, object], schema)
        normalized: dict[str, object] = {}
        for key, value in schema_dict.items():
            if key == "default":
                continue
            normalized[key] = _make_openai_strict_schema(value)
        if "$ref" in normalized:
            return {"$ref": normalized["$ref"]}
        if isinstance(normalized.get("properties"), dict):
            properties = cast(dict[str, object], normalized["properties"])
            normalized["required"] = list(properties.keys())
            normalized["additionalProperties"] = False
        for key in ("anyOf", "oneOf", "allOf"):
            branches = normalized.get(key)
            if isinstance(branches, list):
                normalized[key] = [
                    _normalize_openai_anyof_branch(branch)
                    for branch in cast(list[object], branches)
                ]
        return normalized
    if isinstance(schema, list):
        schema_list = cast(list[object], schema)
        return [_make_openai_strict_schema(item) for item in schema_list]
    return schema


def _normalize_openai_anyof_branch(branch: object) -> object:
    if not isinstance(branch, dict):
        return branch
    normalized: dict[str, object] = dict(cast(Mapping[str, object], branch))
    if not normalized:
        return {"type": "string"}
    if (
        "type" not in normalized
        and "$ref" not in normalized
        and "properties" not in normalized
        and "items" not in normalized
        and "enum" not in normalized
        and "const" not in normalized
    ):
        normalized["type"] = "string"
        return normalized
    return normalized


def _validate_openai_response_schema(schema: object) -> None:
    ref_nodes = _find_ref_nodes_with_forbidden_siblings(schema)
    if ref_nodes:
        path = ".".join(ref_nodes[0]) or "<root>"
        raise ConversationStateTransitionAdjudicatorError(
            error_code="conversation_state_transition_invalid_schema",
            message=(
                "OpenAI response schema contains an invalid $ref sibling keyword at "
                f"{path}."
            ),
            reason_code="invalid_transition_response_schema",
            context={"schema_path": path, "forbidden_keyword": "default"},
        )
    default_nodes = _find_schema_nodes_with_key(schema, "default")
    if default_nodes:
        path = ".".join(default_nodes[0]) or "<root>"
        raise ConversationStateTransitionAdjudicatorError(
            error_code="conversation_state_transition_invalid_schema",
            message=f"OpenAI response schema contains an unsupported default at {path}.",
            reason_code="invalid_transition_response_schema",
            context={"schema_path": path, "forbidden_keyword": "default"},
        )


def _find_ref_nodes_with_forbidden_siblings(
    schema: object,
    *,
    path: tuple[str, ...] = (),
) -> list[tuple[str, ...]]:
    nodes: list[tuple[str, ...]] = []
    if isinstance(schema, dict):
        schema_dict = cast(dict[str, object], schema)
        if "$ref" in schema_dict and "default" in schema_dict:
            nodes.append(path)
        for key, value in schema_dict.items():
            nodes.extend(
                _find_ref_nodes_with_forbidden_siblings(value, path=path + (str(key),))
            )
    elif isinstance(schema, list):
        schema_list = cast(list[object], schema)
        for index, value in enumerate(schema_list):
            nodes.extend(
                _find_ref_nodes_with_forbidden_siblings(value, path=path + (str(index),))
            )
    return nodes


def _find_schema_nodes_with_key(
    schema: object,
    key_name: str,
    *,
    path: tuple[str, ...] = (),
) -> list[tuple[str, ...]]:
    nodes: list[tuple[str, ...]] = []
    if isinstance(schema, dict):
        schema_dict = cast(dict[str, object], schema)
        if key_name in schema_dict:
            nodes.append(path)
        for key, value in schema_dict.items():
            nodes.extend(_find_schema_nodes_with_key(value, key_name, path=path + (str(key),)))
    elif isinstance(schema, list):
        schema_list = cast(list[object], schema)
        for index, value in enumerate(schema_list):
            nodes.extend(_find_schema_nodes_with_key(value, key_name, path=path + (str(index),)))
    return nodes


def _status_code_from_error(error: APIStatusError) -> int | None:
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    return None


def _transition_status_error(
    *,
    error: APIStatusError,
    status_code: int,
    attempt: int,
) -> ConversationStateTransitionAdjudicatorError:
    if status_code == 400:
        return ConversationStateTransitionAdjudicatorError(
            error_code="conversation_state_transition_invalid_schema",
            message="OpenAI rejected the transition response schema.",
            reason_code="invalid_transition_response_schema",
            context={
                "attempt": attempt,
                "status_code": status_code,
                "error_type": type(error).__name__,
            },
        )
    if status_code in {401, 403}:
        return ConversationStateTransitionAdjudicatorError(
            error_code="conversation_state_transition_api_error",
            message="OpenAI authentication or permission failure during transition adjudication.",
            reason_code="transition_auth_error",
            context={
                "attempt": attempt,
                "status_code": status_code,
                "error_type": type(error).__name__,
            },
        )
    return ConversationStateTransitionAdjudicatorError(
        error_code="conversation_state_transition_api_error",
        message=f"API error during transition adjudication: {error}",
        reason_code="transition_api_error",
        context={
            "attempt": attempt,
            "status_code": status_code,
            "error_type": type(error).__name__,
        },
    )
