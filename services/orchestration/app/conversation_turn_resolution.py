"""Structured semantic resolution for one conversational turn.

This module intentionally contains no conversational keyword heuristics.  The
model supplies meaning; this module supplies contracts, transport and
deterministic safety validation.
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportOptionalMemberAccess=false, reportUnusedFunction=false
from __future__ import annotations

import json
from collections.abc import Sequence
from enum import Enum
from typing import Any, Literal, Protocol, cast

from openai import OpenAI
from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError
from services.orchestration.app.request_timer import timed_print

TURN_RESOLUTION_SCHEMA_VERSION = "2026-07-28"
TURN_RESOLUTION_PROMPT_VERSION = "2026-07-28"
_OPENAI_MAX_TOKENS = 1200


class TurnRelationship(str, Enum):
    standalone = "standalone"; continuation = "continuation"; refinement = "refinement"
    correction = "correction"; comparison = "comparison"; topic_shift = "topic_shift"
    clarification_answer = "clarification_answer"; meta_conversation = "meta_conversation"
    cancellation_or_retraction = "cancellation_or_retraction"; replay = "replay"
    artifact_transformation = "artifact_transformation"; result_transformation = "result_transformation"


class TurnOperationMode(str, Enum):
    informational = "informational"; computation = "computation"; action = "action"; artifact = "artifact"; meta = "meta"


class TurnAnswerability(str, Enum):
    answerable = "answerable"; answerable_with_assumptions = "answerable_with_assumptions"
    clarification_required = "clarification_required"; unsupported_action = "unsupported_action"


class ConversationTurnCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str; execution_id: str | None; role: Literal["user", "assistant", "system_outcome"]
    prompt_text: str | None; answer_summary: str | None; intent_class: str | None
    tax_domain_hint: str | None; tax_year: int | None; selected_route: dict[str, str] | None
    turn_outcome_kind: str | None; clarification_requested_fields: list[str]; created_at: str | None


class ResolvedConversationReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mention: str; resolved_value: str; source_candidate_id: str; source_execution_id: str | None; confidence: float


class ConversationTurnResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]; relationship: TurnRelationship; operation_mode: TurnOperationMode
    raw_prompt: str; contextualized_prompt: str; intent_class: str; tax_domain_hint: str | None
    retrieval_tax_domain_filter: str | None; jurisdiction_hint: str | None; tax_year_hint: int | None
    supported_lane_id: str | None = None; historical_version_id: str | None = None; regime_identifier: str | None = None
    answerability: TurnAnswerability; clarification_reason_code: str | None; clarification_question: str | None
    candidate_service_families: list[str] = []
    required_context_fields: list[str]; needs_knowledge_retrieval: bool; needs_computation: bool
    needs_external_action: bool; needs_artifact_operation: bool; referenced_candidate_ids: list[str]
    resolved_references: list[ResolvedConversationReference]; retained_fields: list[str]; corrected_fields: list[str]
    reuse_prior_semantic_facts: bool; reuse_prior_computation_result: bool; reuse_prior_evidence: bool
    reuse_prior_artifact: bool; assumptions: list[str]; confidence: float; audit_summary: str
    provided_context_fields: list[str]; missing_required_context_fields: list[str]


class ConversationTurnResolutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    today: str; trusted_jurisdiction: str | None; tenant_product_context: dict[str, object]
    current_prompt: str; recent_candidates: list[ConversationTurnCandidate]; supported_intents: list[str]
    supported_knowledge_domains: list[str]; supported_computations: list[str]; supported_artifact_operations: list[str]
    external_action_considered: bool; immediately_preceding_clarification: dict[str, object] | None
    prior_failure_metadata: dict[str, object] | None


class ConversationTurnResolutionError(RuntimeError):
    def __init__(self, *, error_code: str, reason_code: str, message: str) -> None:
        super().__init__(message); self.error_code = error_code; self.reason_code = reason_code; self.message = message


class ConversationTurnResolver(Protocol):
    def resolve_turn(self, payload: ConversationTurnResolutionInput) -> ConversationTurnResolution: ...


_SYSTEM_PROMPT = """You resolve conversational meaning for a Kenyan tax assistant. Return only the strict schema; do not answer the user.
A request is standalone unless prior context is semantically necessary. Words such as it, this, that, and, earlier, again, or what about do not alone establish a follow-up; pronouns can be resolved within the current message. Use only supplied candidate IDs. Recent candidates are authoritative conversational context: when a prior turn established a concrete salary, amount, year, deadline, result, or filing subject, resolve later references to that context from the supplied candidates instead of asking for clarification. Standalone and unrelated topic shifts have empty references.
Relationships: continuation/refinement build on a prior turn; correction explicitly replaces a prior fact; comparison compares prior results; topic_shift starts another subject without reuse; clarification_answer answers the immediately preceding explicit clarification; meta_conversation discusses the interaction; replay repeats an earlier request; transformations alter an artifact or result. A VAT discussion followed by a married-filing question is a topic shift, not VAT reuse. Explicit corrections preserve the goal and replace the prior inferred fact rather than reopening the whole topic. Follow-up detail questions about the same subject, its consequences, its supporting law, or a narrower aspect of the prior answer are continuation or refinement, not clarification_answer. Never use clarification_answer for an ordinary follow-up, restatement, or correction; reserve it only for a direct answer to an immediately preceding clarification question. If immediately_preceding_clarification is null, clarification_answer is not available and the turn should instead be continuation, refinement, or correction as appropriate.
Clarify only when a required fact materially changes a computation/action and cannot safely be assumed. Informational questions should normally be answerable or answerable_with_assumptions. Unfamiliar tax terms use broad general_tax retrieval, not a clarification. Trusted jurisdiction is Kenya. For clarification_required supply reason, question and one or more fields; otherwise clear all clarification fields.
If answerability is answerable or answerable_with_assumptions, required_context_fields must be empty. Use answerable_with_assumptions only when conservative assumptions are explicitly helpful and include those assumptions. Do not mix answerable with clarification metadata.
Contextualized_prompt is a complete, natural, conservative request. Keep it concise and do not overload it with unnecessary dates or assumptions from older context unless the current question truly needs them. Never perform token substitution or invent facts. Example: VAT then 'Which acts govern it?' becomes 'Which laws govern VAT in Kenya?'. 'My husband and I got married earlier this year. Should we file together?' remains about filing, not VAT. 'What about fish tax?' is lookup_grounded_knowledge/general_tax with retrieval filter null and an assumption that it may mean tax, levy, licence fee or VAT treatment. 'Calculate my tax for 2026' requires taxable_income_or_income_components. Feedback about junk is meta. 'I earned freelance income. Do I declare it?' is standalone."""


class OpenAIConversationTurnResolver:
    def __init__(self, *, client: OpenAI, model: str) -> None: self._client, self._model = client, model
    def resolve_turn(self, payload: ConversationTurnResolutionInput) -> ConversationTurnResolution:
        last_transport_error: Exception | None = None
        last_validation_error: ConversationTurnResolutionError | None = None
        repair_instruction: str | None = None
        for attempt in range(2):
            try:
                timed_print("[TURN_RESOLVER] About to invoke OpenAI turn-resolution transport")
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=cast(Any, _build_messages(payload, repair_instruction=repair_instruction)),
                    temperature=0,
                    max_tokens=_OPENAI_MAX_TOKENS,
                    response_format=cast(Any, _build_response_format()),
                )
                timed_print("[TURN_RESOLVER] Completed OpenAI turn-resolution transport")
            except Exception as error:
                last_transport_error = error
                timed_print("[TURN_RESOLVER] OpenAI turn-resolution transport failed")
                continue
            content = getattr(getattr(response.choices[0], "message", None), "content", None) if response.choices else None
            if not isinstance(content, str) or not content.strip():
                raise ConversationTurnResolutionError(error_code="empty_llm_response", reason_code="empty_llm_response", message="Conversation turn resolution returned an empty response.")
            try:
                timed_print("[TURN_RESOLVER] About to parse structured turn-resolution response")
                result = ConversationTurnResolution.model_validate_json(content)
                timed_print(
                    "[TURN_RESOLVER] Parsed structured turn-resolution response "
                    f"choice_count={len(response.choices)}"
                )
            except ValidationError as error:
                raise ConversationTurnResolutionError(error_code="turn_resolution_invalid_response", reason_code="turn_resolution_invalid_response", message="Conversation turn resolution returned malformed structured data.") from error
            timed_print("[TURN_RESOLVER] About to normalize validated turn-resolution response")
            result = _normalize_openai_turn_resolution(result)
            timed_print("[TURN_RESOLVER] About to validate normalized turn-resolution response")
            try:
                validate_conversation_turn_resolution(resolution=result, input_payload=payload)
            except ConversationTurnResolutionError as error:
                last_validation_error = error
                if error.reason_code == "turn_resolution_invalid_clarification_answer" and attempt == 0:
                    repair_instruction = (
                        "The previous resolution was invalid because clarification_answer is only "
                        "available when the immediately preceding turn was an explicit clarification. "
                        "Resolve the same payload again and classify an ordinary follow-up as "
                        "continuation or refinement, or a self-correction as correction."
                    )
                    timed_print("[TURN_RESOLVER] Retrying semantic turn resolution after invalid clarification_answer")
                    continue
                if error.reason_code == "turn_resolution_invalid_clarification_answer" and payload.immediately_preceding_clarification is None:
                    fallback_update: dict[str, object] = {
                        "relationship": TurnRelationship.continuation,
                        "clarification_reason_code": None,
                        "clarification_question": None,
                        "required_context_fields": [],
                    }
                    if not result.referenced_candidate_ids and payload.recent_candidates:
                        fallback_update["referenced_candidate_ids"] = [
                            payload.recent_candidates[-1].candidate_id
                        ]
                    repaired_result = result.model_copy(update=fallback_update)
                    validate_conversation_turn_resolution(
                        resolution=repaired_result,
                        input_payload=payload,
                    )
                    timed_print(
                        "[TURN_RESOLVER] Repaired invalid clarification_answer as continuation"
                    )
                    return repaired_result
                if error.reason_code in {
                    "turn_resolution_missing_candidate_reference",
                    "turn_resolution_unknown_candidate_reference",
                } and payload.recent_candidates:
                    latest_candidate_id = payload.recent_candidates[-1].candidate_id
                    repaired_result = result.model_copy(
                        update={
                            "referenced_candidate_ids": [latest_candidate_id],
                            "resolved_references": [],
                        }
                    )
                    validate_conversation_turn_resolution(
                        resolution=repaired_result,
                        input_payload=payload,
                    )
                    timed_print(
                        "[TURN_RESOLVER] Repaired missing candidate reference from recent context"
                    )
                    return repaired_result
                raise
            timed_print("[TURN_RESOLVER] Validated normalized turn-resolution response")
            return result
        if last_validation_error is not None:
            raise last_validation_error
        raise ConversationTurnResolutionError(error_code="turn_resolution_failed", reason_code="turn_resolution_provider_failure", message="Conversation turn resolution is temporarily unavailable.") from last_transport_error


def build_default_turn_resolver(client: OpenAI | None = None, model: str | None = None) -> ConversationTurnResolver:
    if client is None or not model:
        raise ConversationTurnResolutionError(error_code="conversation_turn_resolver_not_configured", reason_code="conversation_turn_resolver_not_configured", message="Conversation turn resolver configuration is required.")
    return OpenAIConversationTurnResolver(client=client, model=model)


def validate_conversation_turn_resolution(*, resolution: ConversationTurnResolution, input_payload: ConversationTurnResolutionInput) -> None:
    allowed = {candidate.candidate_id for candidate in input_payload.recent_candidates}
    refs = resolution.referenced_candidate_ids
    if len(refs) != len(set(refs)): _invalid("turn_resolution_duplicate_candidate_reference")
    if any(item not in allowed for item in refs): _invalid("turn_resolution_unknown_candidate_reference")
    seen_references: dict[str, str] = {}
    for reference in resolution.resolved_references:
        if reference.source_candidate_id not in allowed or reference.source_candidate_id not in refs: _invalid("turn_resolution_unknown_candidate_reference")
        previous = seen_references.get(reference.source_candidate_id)
        if previous is not None and previous != reference.resolved_value: _invalid("turn_resolution_conflicting_reference_output")
        seen_references[reference.source_candidate_id] = reference.resolved_value
    reuse = any((resolution.reuse_prior_semantic_facts, resolution.reuse_prior_computation_result, resolution.reuse_prior_evidence, resolution.reuse_prior_artifact))
    if resolution.relationship == TurnRelationship.standalone and (refs or resolution.resolved_references or reuse): _invalid("turn_resolution_standalone_references_prior_context")
    if resolution.relationship == TurnRelationship.topic_shift and reuse: _invalid("turn_resolution_topic_shift_reuses_prior_context")
    if resolution.relationship in {TurnRelationship.continuation, TurnRelationship.refinement, TurnRelationship.replay} and not refs: _invalid("turn_resolution_missing_candidate_reference")
    if resolution.relationship == TurnRelationship.clarification_answer:
        if not input_payload.immediately_preceding_clarification: _invalid("turn_resolution_invalid_clarification_answer")
        requested = set(cast(Sequence[str], input_payload.immediately_preceding_clarification.get("required_context_fields", ())))
        provided = set(resolution.provided_context_fields)
        if not requested or not provided or not (requested & provided): _invalid("turn_resolution_invalid_clarification_answer")
    if resolution.answerability == TurnAnswerability.clarification_required:
        if not (resolution.clarification_reason_code and resolution.clarification_question and resolution.required_context_fields): _invalid("turn_resolution_invalid_clarification_metadata")
    elif resolution.clarification_reason_code or resolution.clarification_question or resolution.required_context_fields: _invalid("turn_resolution_stale_clarification_metadata")
    if resolution.answerability == TurnAnswerability.answerable_with_assumptions and not resolution.assumptions: _invalid("turn_resolution_missing_assumptions")
    if resolution.answerability == TurnAnswerability.answerable and resolution.assumptions: _invalid("turn_resolution_unexpected_assumptions")
    if resolution.answerability in {TurnAnswerability.answerable, TurnAnswerability.answerable_with_assumptions} and resolution.required_context_fields: _invalid("turn_resolution_stale_clarification_metadata")
    if resolution.operation_mode == TurnOperationMode.meta and (resolution.needs_computation or resolution.needs_external_action or resolution.needs_artifact_operation): _invalid("turn_resolution_invalid_meta_operation")
    if resolution.operation_mode == TurnOperationMode.computation and not resolution.needs_computation: _invalid("turn_resolution_invalid_computation_operation")
    if resolution.operation_mode == TurnOperationMode.action and not resolution.needs_external_action: _invalid("turn_resolution_invalid_action_operation")
    if resolution.operation_mode == TurnOperationMode.artifact and not resolution.needs_artifact_operation: _invalid("turn_resolution_invalid_artifact_operation")
    if resolution.operation_mode != TurnOperationMode.meta and resolution.intent_class == "meta_conversation" and resolution.needs_knowledge_retrieval: _invalid("turn_resolution_invalid_meta_operation")


def _normalize_openai_turn_resolution(resolution: ConversationTurnResolution) -> ConversationTurnResolution:
    update: dict[str, object] = {}
    has_prior_reference = bool(
        resolution.referenced_candidate_ids
        or resolution.resolved_references
        or resolution.reuse_prior_semantic_facts
        or resolution.reuse_prior_computation_result
        or resolution.reuse_prior_evidence
        or resolution.reuse_prior_artifact
    )
    if resolution.relationship == TurnRelationship.standalone and has_prior_reference:
        update["relationship"] = TurnRelationship.continuation
    if resolution.answerability in {TurnAnswerability.answerable, TurnAnswerability.answerable_with_assumptions}:
        if resolution.clarification_reason_code is not None:
            update["clarification_reason_code"] = None
        if resolution.clarification_question is not None:
            update["clarification_question"] = None
        if resolution.required_context_fields:
            update["required_context_fields"] = []
        if resolution.answerability == TurnAnswerability.answerable and resolution.assumptions:
            update["answerability"] = TurnAnswerability.answerable_with_assumptions
    if not update:
        return resolution
    return resolution.model_copy(update=update)


def _invalid(reason_code: str) -> None:
    raise ConversationTurnResolutionError(error_code="turn_resolution_validation_failed", reason_code=reason_code, message="Conversation turn resolution failed deterministic validation.")


def _build_response_format() -> dict[str, object]:
    schema = ConversationTurnResolution.model_json_schema(mode="serialization")
    _strip_schema_defaults(schema)
    _require_all_object_properties(schema)
    _validate_turn_resolution_schema(schema)
    return {"type": "json_schema", "json_schema": {"name": "conversation_turn_resolution", "strict": True, "schema": schema}}


def _validate_turn_resolution_schema(value: object) -> None:
    if isinstance(value, dict):
        if "$ref" in value and "default" in value: raise ValueError("schema_ref_default_conflict")
        if "default" in value: raise ValueError("schema_default_not_allowed")
        if value.get("type") == "object" and value.get("additionalProperties") is not False: raise ValueError("schema_object_not_strict")
        for item in value.values(): _validate_turn_resolution_schema(item)
    elif isinstance(value, list):
        for item in value: _validate_turn_resolution_schema(item)


def _strip_schema_defaults(value: object) -> None:
    if isinstance(value, dict):
        value.pop("default", None)
        for item in value.values():
            _strip_schema_defaults(item)
    elif isinstance(value, list):
        for item in value:
            _strip_schema_defaults(item)


def _require_all_object_properties(value: object) -> None:
    if isinstance(value, dict):
        if value.get("type") == "object" and isinstance(value.get("properties"), dict):
            properties = cast(dict[str, object], value["properties"])
            value["required"] = sorted(properties.keys())
        for item in value.values():
            _require_all_object_properties(item)
    elif isinstance(value, list):
        for item in value:
            _require_all_object_properties(item)


def _build_messages(payload: ConversationTurnResolutionInput, *, repair_instruction: str | None = None) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    if repair_instruction is not None:
        messages.append({"role": "system", "content": repair_instruction})
    messages.append({"role": "user", "content": json.dumps(payload.model_dump(mode="json"), separators=(",", ":"))})
    return messages
