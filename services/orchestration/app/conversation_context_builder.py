"""Project governed execution outputs into bounded conversation-state context."""

from __future__ import annotations

from typing import cast
from collections.abc import Mapping
from collections.abc import Sequence

from services.orchestration.app.canonical_fact_ledger import FACT_SCHEMA_VERSION
from services.orchestration.app.canonical_fact_ledger import build_canonical_fact_ledger


def build_conversation_state_payload(
    *,
    execution_id: str,
    prompt_text: str,
    prompt_checksum: str,
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
    stated_facts: dict[str, object],
) -> dict[str, object]:
    """Build the bounded reusable conversation context for one execution."""

    canonical_fact_ledger = [
        fact.model_dump(mode="python")
        for fact in build_canonical_fact_ledger(
            stated_facts=stated_facts,
            origin_execution_id=execution_id,
            origin_record_id=None,
            source_status="explicit",
            turn_sequence=0,
        )
    ]
    return {
        "conversation_state_schema_version": "2026-07-26",
        "execution_id": execution_id,
        "prompt_text": prompt_text,
        "prompt_checksum": prompt_checksum,
        "intent_class": intent_class,
        "tax_domain_hint": tax_domain_hint,
        "selected_route": dict(selected_route) if selected_route is not None else None,
        "plan_summary": {
            "plan_id": plan.get("plan_id"),
            "plan_status": plan.get("plan_status"),
            "planning_mode": plan.get("planning_mode"),
            "execution_ready": plan.get("execution_ready"),
        },
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "regime_identifier": regime_identifier,
        "tax_year": tax_year,
        "mapped_result_summary": {
            "action_status": mapped_result.get("action_status"),
            "reason_code": mapped_result.get("reason_code"),
            "provider_reference": mapped_result.get("provider_reference"),
        },
        "assistant_answer_summary": (
            str(mapped_result.get("message"))
            if isinstance(mapped_result.get("message"), str)
            else None
        ),
        "prior_answer_summary": (
            str(mapped_result.get("message"))
            if isinstance(mapped_result.get("message"), str)
            else None
        ),
        "assistant_turn_kind": "answer",
        "turn_outcome_kind": "execution_success",
        "adapter_result_payload": (
            dict(cast(Mapping[str, object], adapter_response.get("result_payload")))
            if adapter_response is not None
            and isinstance(adapter_response.get("result_payload"), Mapping)
            else None
        ),
        "grounded_citation_summary": _project_citation_summary(citations),
        "grounded_evidence_summary": _project_grounded_evidence_summary(grounded_evidence),
        "stated_facts": stated_facts,
        "canonical_fact_ledger": canonical_fact_ledger,
        "fact_schema_version": FACT_SCHEMA_VERSION,
        "service_artifact_summary": _project_service_artifact_summary(
            selected_route=selected_route,
            adapter_response=adapter_response,
        ),
    }


def build_followup_conversation_summary(
    *,
    state_payload: Mapping[str, object],
    followup_mode: str,
    reused_fields: Sequence[str],
) -> dict[str, object]:
    """Build the bounded same-conversation summary exposed to answer synthesis."""

    selected_route = state_payload.get("selected_route")
    return {
        "same_conversation_reuse": True,
        "prior_execution_id": state_payload.get("execution_id"),
        "prior_intent_class": state_payload.get("intent_class"),
        "prior_tax_domain_hint": state_payload.get("tax_domain_hint"),
        "prior_answer_summary": state_payload.get("assistant_answer_summary"),
        "prior_failure_summary": state_payload.get("failure_summary"),
        "prior_failure_reason_code": state_payload.get("reason_code"),
        "prior_selected_route": (
            dict(cast(Mapping[str, object], selected_route))
            if isinstance(selected_route, Mapping)
            else None
        ),
        "followup_mode": followup_mode,
        "reused_fields": [str(item) for item in reused_fields],
    }


def _project_citation_summary(
    citations: Sequence[Mapping[str, object]] | None,
) -> list[dict[str, object]]:
    if citations is None:
        return []
    projected: list[dict[str, object]] = []
    for item in citations:
        projected.append(
            {
                "citation_index": item.get("citation_index"),
                "source_id": item.get("source_id"),
                "source_version_id": item.get("source_version_id"),
                "anchor_id": item.get("anchor_id"),
                "title": item.get("title"),
                "authority_level": item.get("authority_level"),
                "temporal_applicability": item.get("temporal_applicability"),
            }
        )
    return projected


def _project_grounded_evidence_summary(
    grounded_evidence: Sequence[Mapping[str, object]] | None,
) -> list[dict[str, object]]:
    if grounded_evidence is None:
        return []
    projected: list[dict[str, object]] = []
    for item in grounded_evidence:
        projected.append(
            {
                "source_id": item.get("source_id"),
                "source_version_id": item.get("source_version_id"),
                "anchor_id": item.get("anchor_id"),
                "title": item.get("title"),
                "authority_level": item.get("authority_level"),
                "effective_from": item.get("effective_from"),
                "effective_to": item.get("effective_to"),
                "tax_year": item.get("tax_year"),
            }
        )
    return projected


def _project_service_artifact_summary(
    *,
    selected_route: Mapping[str, object] | None,
    adapter_response: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if selected_route is None or adapter_response is None:
        return None
    result_payload = adapter_response.get("result_payload")
    if not isinstance(result_payload, Mapping):
        return None
    target_service = selected_route.get("target_service")
    normalized_payload = cast(Mapping[str, object], result_payload)
    if target_service == "forms":
        return {
            "service": "forms",
            "artifact_id": normalized_payload.get("artifact_id"),
            "form_ready_reference": normalized_payload.get("form_ready_reference"),
            "form_type": normalized_payload.get("form_type"),
            "form_version_id": normalized_payload.get(
                "form_version_id",
                normalized_payload.get("form_version"),
            ),
            "tax_year": normalized_payload.get("tax_year"),
            "supported_lane_id": normalized_payload.get("supported_lane_id"),
            "historical_version_id": normalized_payload.get("historical_version_id"),
        }
    if target_service == "reports":
        return {
            "service": "reports",
            "report_id": normalized_payload.get("report_id"),
            "report_type": normalized_payload.get("report_type"),
            "report_version_id": normalized_payload.get("report_version_id"),
            "tax_year": normalized_payload.get("tax_year"),
        }
    if target_service == "document_ai":
        return {
            "service": "document_ai",
            "document_reference": normalized_payload.get("document_reference"),
            "document_id": normalized_payload.get("document_id"),
            "lifecycle_status": normalized_payload.get("lifecycle_status"),
            "operation": normalized_payload.get("operation"),
            "evidence_limitations": normalized_payload.get("evidence_limitations", []),
            "evidence": normalized_payload.get("evidence"),
        }
    return None
