"""Deterministic binding of an already resolved conversational turn."""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportOptionalSubscript=false, reportUnusedFunction=false, reportArgumentType=false
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NotRequired, TypedDict, cast

from services.orchestration.app.conversation_context_builder import build_followup_conversation_summary
from services.orchestration.app.conversation_state_store import ConversationStateRecord
from services.orchestration.app.conversation_turn_resolution import ConversationTurnCandidate, ConversationTurnResolution


class FollowupResolutionResult(TypedDict):
    effective_prompt_text: str; followup_mode: str; conversation_context_summary: dict[str, object]
    prior_execution_id: str | None; referenced_execution_ids: list[str]; primary_referenced_execution_id: str | None
    reused_fields: tuple[str, ...]; reuse_prior_service_result: bool
    knowledge_route_payload: NotRequired[dict[str, object] | None]; updated_semantic_frame: NotRequired[dict[str, object]]
    reused_semantic_facts_payload: NotRequired[dict[str, object] | None]
    reused_computation_result_payload: NotRequired[dict[str, object] | None]
    reused_evidence_payload: NotRequired[list[dict[str, object]] | None]
    reused_artifact_payload: NotRequired[dict[str, object] | None]
    reused_service_result_payload: NotRequired[dict[str, object] | None]
    turn_resolution: dict[str, object]


class FollowupResolutionError(RuntimeError):
    """Retained only for state-store compatibility at the runtime boundary."""


def build_bounded_candidates(records: Sequence[ConversationStateRecord]) -> list[ConversationTurnCandidate]:
    """Return chronological role-separated, bounded candidates and no hidden facts."""
    result: list[ConversationTurnCandidate] = []
    for record in reversed(records[:8]):
        payload = record["context_payload"]; execution_id = record["execution_id"]; base = str(payload.get("record_id", execution_id))
        common = dict(execution_id=execution_id, intent_class=_text(payload.get("intent_class")), tax_domain_hint=_text(payload.get("tax_domain_hint")), tax_year=_integer(payload.get("tax_year_hint") or payload.get("tax_year")), selected_route=_route(payload.get("selected_route")), turn_outcome_kind=_text(payload.get("turn_outcome_kind")), clarification_requested_fields=_fields(payload), created_at=_text(payload.get("created_at")))
        raw = _text(payload.get("raw_prompt_text")) or _text(payload.get("prompt_text"))
        if raw: result.append(ConversationTurnCandidate(candidate_id=f"{base}:user", role="user", prompt_text=raw, answer_summary=None, **common))
        answer = _text(payload.get("assistant_answer_summary")) or _text(payload.get("clarification_question")) or _text(payload.get("answer_summary")) or _text(payload.get("user_facing_message"))
        if answer: result.append(ConversationTurnCandidate(candidate_id=f"{base}:assistant", role="assistant", prompt_text=None, answer_summary=answer, **common))
        if _text(payload.get("turn_outcome_kind")) in {"execution_failed", "system_failure", "failure"}: result.append(ConversationTurnCandidate(candidate_id=f"{base}:system-outcome", role="system_outcome", prompt_text=None, answer_summary=_text(payload.get("failure_summary")) or answer, **common))
    return result


def immediately_preceding_clarification(records: Sequence[ConversationStateRecord]) -> dict[str, object] | None:
    if not records: return None
    payload = records[0]["context_payload"]
    if payload.get("assistant_turn_kind") != "clarification": return None
    fields = _fields(payload)
    if not fields: return None
    return {"execution_id": records[0]["execution_id"], "required_context_fields": fields, "clarification_reason_code": _text(payload.get("clarification_reason_code")), "clarification_question": _text(payload.get("clarification_question")), "intent_class": _text(payload.get("intent_class")), "tax_domain_hint": _text(payload.get("tax_domain_hint"))}


def build_followup_resolution(*, turn_resolution: ConversationTurnResolution, recent_conversation_state: Sequence[ConversationStateRecord], current_semantic_frame: Mapping[str, object] | None = None) -> FollowupResolutionResult | None:
    """Bind only exact model-authorized candidate records; never infer semantics."""
    bindings: dict[str, ConversationStateRecord] = {}
    for record in recent_conversation_state[:8]:
        base = str(record["context_payload"].get("record_id", record["execution_id"]))
        bindings.update({f"{base}:user": record, f"{base}:assistant": record, f"{base}:system-outcome": record})
    selected = [bindings[item] for item in turn_resolution.referenced_candidate_ids if item in bindings]
    unique: list[ConversationStateRecord] = []
    for record in selected:
        if record not in unique: unique.append(record)
    ids = [record["execution_id"] for record in unique]
    if not ids and not any((turn_resolution.reuse_prior_semantic_facts, turn_resolution.reuse_prior_computation_result, turn_resolution.reuse_prior_evidence, turn_resolution.reuse_prior_artifact)):
        return None
    primary = unique[0] if len(unique) == 1 else None
    state = (primary or (unique[0] if unique else None))["context_payload"] if unique else {}
    frame = dict(current_semantic_frame or {})
    frame.update({"prompt_text": turn_resolution.raw_prompt, "effective_prompt_text": turn_resolution.contextualized_prompt, "intent_class": turn_resolution.intent_class, "tax_domain_hint": turn_resolution.tax_domain_hint or "general_tax"})
    reusable_state = state if primary is not None else {}
    return {"effective_prompt_text": turn_resolution.contextualized_prompt, "followup_mode": turn_resolution.relationship.value, "conversation_context_summary": build_followup_conversation_summary(state_payload=state, followup_mode=turn_resolution.relationship.value, reused_fields=tuple(turn_resolution.retained_fields)) if primary is not None else {}, "prior_execution_id": ids[0] if ids else None, "referenced_execution_ids": ids, "primary_referenced_execution_id": ids[0] if len(ids) == 1 else None, "reused_fields": tuple(turn_resolution.retained_fields), "reuse_prior_service_result": bool(turn_resolution.reuse_prior_artifact or turn_resolution.reuse_prior_computation_result), "reused_semantic_facts_payload": cast(dict[str, object] | None, reusable_state.get("stated_facts")) if turn_resolution.reuse_prior_semantic_facts else None, "reused_computation_result_payload": cast(dict[str, object] | None, reusable_state.get("mapped_result_summary")) if turn_resolution.reuse_prior_computation_result else None, "reused_evidence_payload": cast(list[dict[str, object]] | None, reusable_state.get("grounded_evidence_summary")) if turn_resolution.reuse_prior_evidence else None, "reused_artifact_payload": cast(dict[str, object] | None, reusable_state.get("service_artifact_summary")) if turn_resolution.reuse_prior_artifact else None, "reused_service_result_payload": cast(dict[str, object] | None, reusable_state.get("adapter_result_payload")) if turn_resolution.reuse_prior_artifact or turn_resolution.reuse_prior_computation_result else None, "updated_semantic_frame": frame, "turn_resolution": turn_resolution.model_dump(mode="python")}


def _text(value: object) -> str | None: return value.strip() if isinstance(value, str) and value.strip() else None
def _integer(value: object) -> int | None: return value if isinstance(value, int) and not isinstance(value, bool) else None
def _fields(payload: Mapping[str, object]) -> list[str]:
    value = payload.get("clarification_requested_fields", payload.get("required_context_fields", [])); return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
def _route(value: object) -> dict[str, str] | None: return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else None
