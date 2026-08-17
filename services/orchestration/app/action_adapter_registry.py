"""Register deterministic submission action adapters for pilot orchestration."""

# ruff: noqa: E501
# pyright: reportUnusedFunction=false, reportUndefinedVariable=false

from __future__ import annotations

import os
import json
import re
from uuid import UUID
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
from typing import Protocol
from typing import TypedDict
from collections.abc import Mapping
import hashlib
from datetime import date
import warnings
from urllib.error import URLError
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeRepositoryError
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.orchestration.app.debug_trace import bounded_preview
from services.orchestration.app.trace_context import build_trace_id
from services.orchestration.app.debug_trace import emit_orchestration_debug
from services.orchestration.app.tavily_search_client import KRA_LIVE_SOURCES
from services.orchestration.app.tavily_search_client import TavilySearchError
from services.orchestration.app.tavily_search_client import TavilyWebSearchClient
from services.orchestration.app.tavily_search_client import EXTRACT_ELIGIBLE_DOMAINS
from services.orchestration.app.action_adapter_contract import ActionAdapterTrace
from services.orchestration.app.action_adapter_contract import ActionAdapterRequest
from services.orchestration.app.action_adapter_contract import ActionAdapterResponse
from services.orchestration.app.action_adapter_contract import SubmissionActionAdapter
from services.orchestration.app.action_adapter_contract import build_adapter_request_id
from services.orchestration.app.action_adapter_contract import KnowledgeRouteCapability
from services.orchestration.app.action_adapter_contract import build_unsupported_action_response
from services.orchestration.app.action_adapter_contract import (
    dispatch_submission_action_with_adapter,
)
from services.orchestration.app.action_execution_envelope import ActionExecutionRequest
from services.orchestration.app.action_execution_envelope import ActionExecutionEnvelope
from services.orchestration.app.action_execution_envelope import execute_idempotent_action_request
from services.orchestration.app.knowledge_search_intelligence import EXPANSION_TRIGGER_THRESHOLD
from services.orchestration.app.knowledge_search_intelligence import KnowledgeSearchIntelligence
from services.orchestration.app.knowledge_scope_reasoning import analyze_evidence_scope

# Cutoff ages (days) for web-sourced citations by tax domain.
# Citations older than the cutoff are dropped; if the result set falls below
# two items, the next wider tier is tried before giving up.
_FRESHNESS_CUTOFF_DAYS: dict[str, int] = {
    "rates_thresholds": 180,
    "amnesty_waiver": 60,
    "paye_bands": 180,
    "penalties": 365,
    "process_procedural": 365,
    "general_advisory": 548,
}
# Tiers in ascending age order — used for progressive relaxation.
_FRESHNESS_CUTOFF_TIERS = (60, 180, 365, 548, None)
_FRESHNESS_POLICY_BY_DOMAIN_HINT: dict[str, str] = {
    "income_tax": "paye_bands",
    "paye_generalized": "paye_bands",
    "vat": "rates_thresholds",
    "withholding_tax_generalized": "rates_thresholds",
    "business_income_generalized": "rates_thresholds",
    "rental_income_generalized": "rates_thresholds",
    "health_contribution": "process_procedural",
}

def filter_grounded_evidence_for_scope(
    evidence: list[dict[str, object]],
    *,
    tax_domain_hint: str | None,
    resolved_entity: str | None = None,
    query_text: str | None = None,
) -> list[dict[str, object]]:
    """Keep only evidence items that align to both domain and subject scope."""
    emit_orchestration_debug(
        "KNOWLEDGE",
        "scope_filter.start",
        tax_domain_hint=tax_domain_hint,
        resolved_entity=resolved_entity,
        query=query_text,
        evidence_count=len(evidence),
    )
    if not tax_domain_hint:
        emit_orchestration_debug(
            "KNOWLEDGE",
            "scope_filter.skipped",
            tax_domain_hint=tax_domain_hint,
            reason_code="missing_tax_domain_hint",
            evidence_count=len(evidence),
        )
        return evidence

    filtered: list[dict[str, object]] = []
    for item in evidence:
        analysis = analyze_evidence_scope(
            item,
            tax_domain_hint=tax_domain_hint,
            resolved_entity=resolved_entity,
            query_text=query_text,
        )
        scoped = dict(item)
        scoped["tax_domain"] = tax_domain_hint
        scoped["scope_diagnostic"] = analysis["diagnostic"]
        scoped["scope_decision"] = analysis["decision"]
        emit_orchestration_debug(
            "KNOWLEDGE",
            "scope_filter.item",
            source_id=analysis["source_id"],
            source_version_id=analysis["source_version_id"],
            anchor_id=analysis["anchor_id"],
            title=analysis["title"],
            declared_tax_domain=analysis["declared_tax_domain"],
            requested_tax_domain=analysis["requested_tax_domain"],
            resolved_entity=analysis["resolved_entity"],
            normalized_entity=analysis["normalized_entity"],
            domain_markers=analysis["domain_markers"],
            title_marker_matches=analysis["title_marker_matches"],
            title_entity_matches=analysis["title_entity_matches"],
            marker_passage_indices=analysis["marker_passage_indices"],
            entity_passage_indices=analysis["entity_passage_indices"],
            matching_passage_indices=analysis["matching_passage_indices"],
            canonical_claim_count=analysis["canonical_claim_count"],
            decision=analysis["decision"],
            diagnostic=analysis["diagnostic"],
        )
        if analysis["decision"] != "retained":
            continue
        text = _evidence_text(item)
        selected = text if text.strip() else _safe_item_text(item)
        scoped = dict(item)
        scoped["tax_domain"] = tax_domain_hint
        scoped["scope_diagnostic"] = analysis["diagnostic"]
        scoped["scope_decision"] = analysis["decision"]
        if "content" in scoped:
            scoped["content"] = selected
        if "content_excerpt" in scoped:
            scoped["content_excerpt"] = selected
        scoped["domain_relevance"] = analysis["diagnostic"]
        filtered.append(scoped)
    emit_orchestration_debug(
        "KNOWLEDGE",
        "scope_filter.completed",
        tax_domain_hint=tax_domain_hint,
        input_count=len(evidence),
        output_count=len(filtered),
    )
    if not filtered:
        emit_orchestration_debug(
            "KNOWLEDGE",
            "scope_filter.empty",
            tax_domain_hint=tax_domain_hint,
            resolved_entity=resolved_entity,
            query=query_text,
            input_count=len(evidence),
        )
    return filtered


def _evidence_text(item: Mapping[str, object]) -> str:
    parts: list[str] = []
    title = item.get("title")
    if isinstance(title, str) and title.strip():
        parts.append(title)
    for field in ("content", "content_excerpt"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value)
            break
    return "\n".join(parts)


def _safe_item_text(item: Mapping[str, object]) -> str:
    for field in ("content", "content_excerpt", "title"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _split_evidence_passages(text: str) -> list[str]:
    # Newlines model extracted page sections; sentence splitting is the
    # fallback for Tavily snippets that arrive as a single paragraph.
    chunks = [chunk.strip() for chunk in re.split(r"\n{1,}|(?<=[.!?])\s+", text) if chunk.strip()]
    return chunks or [text]


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _filter_evidence_by_freshness(
    evidence: list[dict[str, object]],
    tax_domain_hint: str | None,
) -> list[dict[str, object]]:
    """Drop stale citations; relax cutoff if fewer than two survive."""
    if not tax_domain_hint:
        emit_orchestration_debug(
            "KNOWLEDGE",
            "freshness_filter.skipped",
            runtime_tax_domain=tax_domain_hint,
            reason_code="missing_tax_domain_hint",
            input_count=len(evidence),
        )
        return evidence

    policy_key, base_cutoff = _resolve_freshness_policy(tax_domain_hint)
    if base_cutoff is None:
        emit_orchestration_debug(
            "KNOWLEDGE",
            "freshness_filter.skipped",
            runtime_tax_domain=tax_domain_hint,
            resolved_policy_key=policy_key,
            reason_code="missing_freshness_policy",
            input_count=len(evidence),
        )
        return evidence

    emit_orchestration_debug(
        "KNOWLEDGE",
        "freshness_filter.policy_resolved",
        runtime_tax_domain=tax_domain_hint,
        resolved_policy_key=policy_key,
        base_cutoff_days=base_cutoff,
        input_count=len(evidence),
    )

    # Build tiers: start at the domain cutoff, then widen until >= 2 survive
    # or there are no more tiers to try.
    today = date.today()
    start_index = next(
        (i for i, t in enumerate(_FRESHNESS_CUTOFF_TIERS) if t is not None and t >= base_cutoff),
        0,
    )
    tiers_to_try = _FRESHNESS_CUTOFF_TIERS[start_index:]

    for cutoff in tiers_to_try:
        filtered = _apply_cutoff(evidence, today, cutoff)
        emit_orchestration_debug(
            "KNOWLEDGE",
            "freshness_filter.tier",
            runtime_tax_domain=tax_domain_hint,
            resolved_policy_key=policy_key,
            attempted_cutoff_days=cutoff,
            input_count=len(evidence),
            output_count=len(filtered),
            unknown_date_count=_count_unknown_dates(evidence),
            stale_count=_count_stale_dates(evidence, today, cutoff),
        )
        if len(filtered) >= 2:
            emit_orchestration_debug(
                "KNOWLEDGE",
                "freshness_filter.completed",
                runtime_tax_domain=tax_domain_hint,
                resolved_policy_key=policy_key,
                base_cutoff_days=base_cutoff,
                attempted_cutoff_days=cutoff,
                input_count=len(evidence),
                output_count=len(filtered),
                unknown_date_count=_count_unknown_dates(evidence),
                stale_count=_count_stale_dates(evidence, today, cutoff),
            )
            return filtered

    # All tiers exhausted — return whatever survived, even if < 2.
    filtered = _apply_cutoff(evidence, today, None)
    emit_orchestration_debug(
        "KNOWLEDGE",
        "freshness_filter.completed",
        runtime_tax_domain=tax_domain_hint,
        resolved_policy_key=policy_key,
        base_cutoff_days=base_cutoff,
        attempted_cutoff_days=None,
        input_count=len(evidence),
        output_count=len(filtered),
        unknown_date_count=_count_unknown_dates(evidence),
        stale_count=_count_stale_dates(evidence, today, base_cutoff),
    )
    return filtered


def _resolve_freshness_policy(tax_domain_hint: str) -> tuple[str, int | None]:
    policy_key = _FRESHNESS_POLICY_BY_DOMAIN_HINT.get(tax_domain_hint, "general_advisory")
    return policy_key, _FRESHNESS_CUTOFF_DAYS.get(policy_key)


def _apply_cutoff(
    evidence: list[dict[str, object]],
    today: date,
    cutoff_days: int | None,
) -> list[dict[str, object]]:
    """Keep items whose effective_from is within cutoff_days of today.

    Items with unknown or unparseable dates are always kept so we don't
    silently discard the only available source.
    """
    if cutoff_days is None:
        return evidence

    result: list[dict[str, object]] = []
    for item in evidence:
        effective_from = item.get("effective_from")
        if not isinstance(effective_from, str) or effective_from in ("unknown", ""):
            result.append(item)
            continue
        try:
            item_date = date.fromisoformat(effective_from[:10])
            if (today - item_date).days <= cutoff_days:
                result.append(item)
        except ValueError:
            # Unparseable date — keep the item
            result.append(item)
    return result


def _count_unknown_dates(evidence: list[dict[str, object]]) -> int:
    return sum(
        1
        for item in evidence
        if not isinstance(item.get("effective_from"), str)
        or item.get("effective_from") in ("unknown", "")
    )


def _count_stale_dates(
    evidence: list[dict[str, object]],
    today: date,
    cutoff_days: int | None,
) -> int:
    if cutoff_days is None:
        return 0
    stale = 0
    for item in evidence:
        effective_from = item.get("effective_from")
        if not isinstance(effective_from, str) or effective_from in ("unknown", ""):
            continue
        try:
            item_date = date.fromisoformat(effective_from[:10])
        except ValueError:
            continue
        if (today - item_date).days > cutoff_days:
            stale += 1
    return stale


SUPPORTED_SUBMISSION_ACTION_TYPES = {"submission_execute"}
SUPPORTED_ROUTE_ACTIONS: dict[tuple[str, str], str] = {
    ("tax_core", "execute_computation"): "tax_core_execute_computation",
    ("knowledge", "search_knowledge"): "knowledge_search_knowledge",
    ("knowledge", "retrieve_knowledge"): "knowledge_retrieve_knowledge",
    ("knowledge", "timeline_search_knowledge"): "knowledge_timeline_search_knowledge",
    (
        "forms",
        "generate_income_tax_form_artifact",
    ): "forms_generate_income_tax_form_artifact",
    (
        "forms",
        "map_health_contribution_output_to_form_ready",
    ): "forms_map_health_contribution_output_to_form_ready",
    (
        "reports",
        "create_income_tax_report_artifact",
    ): "reports_create_income_tax_report_artifact",
    (
        "reports",
        "create_health_contribution_report_artifact",
    ): "reports_create_health_contribution_report_artifact",
    ("document_ai", "get_document_processing_status"): "document_ai_get_document_processing_status",
    ("document_ai", "search_document_evidence"): "document_ai_search_document_evidence",
    ("document_ai", "retrieve_document_evidence"): "document_ai_retrieve_document_evidence",
    ("document_ai", "derive_document_evidence"): "document_ai_derive_document_evidence",
    ("document_ai", "create_workflow_evidence_projection"): "document_ai_create_workflow_evidence_projection",
}

SEARCHABLE_KNOWLEDGE_STATES = frozenset({"published", "superseded"})
OFFICIAL_KNOWLEDGE_ORIGINS = frozenset({"official_source_upload", "official_source_url"})


class _KnowledgeEvidenceRecord(TypedDict):
    source_id: str
    source_version_id: str
    anchor_id: str
    title: str
    url: str
    source_type: str
    authority_level: str
    tax_domain: str
    effective_from: str
    effective_to: str | None
    tax_year: int | None
    publication_state: str
    source_version_form: str
    grounding_status: str
    content: str
    canonical_source_ref: str
    knowledge_route_mode: str
    timeline_position: int | None
    canonical_claims: list[dict[str, object]] | None


KNOWLEDGE_ROUTE_CAPABILITY_MANIFEST: dict[str, KnowledgeRouteCapability] = {
    "knowledge_search_route_v1": {
        "route_id": "knowledge_search_route_v1",
        "target_service": "knowledge",
        "target_operation": "search_knowledge",
        "route_mode": "search",
        "preserves_chronology": False,
        "governed_evidence_required": True,
    },
    "knowledge_retrieve_route_v1": {
        "route_id": "knowledge_retrieve_route_v1",
        "target_service": "knowledge",
        "target_operation": "retrieve_knowledge",
        "route_mode": "retrieve",
        "preserves_chronology": False,
        "governed_evidence_required": True,
    },
    "knowledge_timeline_route_v1": {
        "route_id": "knowledge_timeline_route_v1",
        "target_service": "knowledge",
        "target_operation": "timeline_search_knowledge",
        "route_mode": "timeline_search",
        "preserves_chronology": True,
        "governed_evidence_required": True,
    },
}


class KnowledgeRouteRepository(Protocol):
    """Describe governed knowledge repository operations used by orchestration."""

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


def _deterministic_mock_adapter_response(
    *,
    request: ActionAdapterRequest,
    adapter_name: str,
    action_result_code: str,
    message: str,
) -> ActionAdapterResponse:
    trace_id = build_trace_id(request["correlation_id"])
    trace: ActionAdapterTrace = {
        "correlation_id": request["correlation_id"],
        "trace_id": trace_id,
        "adapter_request_id": build_adapter_request_id(
            request=request,
            adapter_name=adapter_name,
        ),
        "adapter_name": adapter_name,
        "submission_payload_ref": request["submission_payload_ref"],
    }
    idempotency_key = request.get("idempotency_key")
    if isinstance(idempotency_key, str):
        trace["idempotency_key"] = idempotency_key
    route_id = request.get("route_id")
    if isinstance(route_id, str):
        trace["route_id"] = route_id
    target_service = request.get("target_service")
    if isinstance(target_service, str):
        trace["target_service"] = target_service
    target_operation = request.get("target_operation")
    if isinstance(target_operation, str):
        trace["target_operation"] = target_operation
    plan_id = request.get("plan_id")
    if isinstance(plan_id, str):
        trace["plan_id"] = plan_id
    step_id = request.get("step_id")
    if isinstance(step_id, str):
        trace["step_id"] = step_id
    return {
        "adapter_status": "mock_pending",
        "provider_reference": None,
        "action_result_code": action_result_code,
        "message": message,
        "trace": trace,
        "error": None,
    }


def _deterministic_accepted_adapter_response(
    *,
    request: ActionAdapterRequest,
    adapter_name: str,
    action_result_code: str,
    message: str,
    provider_reference: str | None,
    result_payload: dict[str, object],
) -> ActionAdapterResponse:
    response = _deterministic_mock_adapter_response(
        request=request,
        adapter_name=adapter_name,
        action_result_code=action_result_code,
        message=message,
    )
    response["adapter_status"] = "accepted"
    response["provider_reference"] = provider_reference
    response["result_payload"] = result_payload
    return response


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _deterministic_uuid(namespace: str) -> str:
    return str(uuid5(NAMESPACE_URL, namespace))


def _deterministic_forms_artifact_payload(
    request: ActionAdapterRequest,
) -> dict[str, object]:
    capability_context = request["capability_context"]
    artifact_seed = build_adapter_request_id(
        request=request,
        adapter_name="deterministic_forms_artifact_payload_v1",
    )
    historical_version_id = capability_context["historical_version_id"]
    supported_lane_id = capability_context["supported_lane_id"]
    return {
        "status": "ok",
        "generation_status": "generated",
        "artifact_id": artifact_seed,
        "artifact_hash": _sha256_hex(f"forms-artifact:{artifact_seed}"),
        "artifact_type": "income_tax_form_artifact",
        "form_type": "income_tax_return",
        "form_version_id": (f"ITX-FORM-{historical_version_id or 'UNBOUND'}-V1"),
        "tax_year": capability_context["tax_year"],
        "historical_version_id": historical_version_id,
        "supported_lane_id": supported_lane_id,
        "immutability_status": "immutable",
        "immutable": True,
    }


def _deterministic_health_form_mapping_payload(
    request: ActionAdapterRequest,
) -> dict[str, object]:
    capability_context = request["capability_context"]
    mapping_reference = build_adapter_request_id(
        request=request,
        adapter_name="deterministic_health_form_mapping_payload_v1",
    )
    historical_version_id = capability_context["historical_version_id"]
    supported_lane_id = capability_context["supported_lane_id"]
    tax_year = capability_context["tax_year"]
    return {
        "mapping_status": "ok",
        "form_type": "health_contribution_summary",
        "form_version": "health_contribution_vertical_slice_v1",
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "form_ready_reference": mapping_reference,
        "version_identity": {
            "historical_version_id": historical_version_id,
            "regime_identifier": "health_contribution",
        },
        "lineage": {
            "computation_status": "computed",
            "replay_safe": True,
        },
        "unsupported_fields": [],
    }


def _deterministic_report_payload(
    request: ActionAdapterRequest,
    *,
    tax_domain: str,
) -> dict[str, object]:
    capability_context = request["capability_context"]
    supported_lane_id = capability_context["supported_lane_id"] or f"{tax_domain}_lane_unbound"
    historical_version_id = (
        capability_context["historical_version_id"] or f"{tax_domain}-hist-unbound"
    )
    tax_year = capability_context["tax_year"] or 0
    report_type = (
        "income_tax_summary" if tax_domain == "income_tax" else "health_contribution_summary"
    )
    report_version_id = f"RPT-{tax_domain.upper()}-{historical_version_id}-V1"
    idempotency_key = str(request.get("idempotency_key", "missing-idempotency-key"))
    report_id = _deterministic_uuid(
        f"orchestration-report:{request['action_type']}:{idempotency_key}"
    )
    return {
        "status": "generated",
        "report_id": report_id,
        "report_type": report_type,
        "tax_year": tax_year,
        "report_version_id": report_version_id,
        "artifact_metadata": {
            "format": "pdf",
            "artifact_kind": "report_pdf",
            "report_id": report_id,
            "report_version_id": report_version_id,
            "content_sha256": _sha256_hex(f"report:{report_id}"),
        },
        "lineage_reference": {
            "computation_id": _deterministic_uuid(f"report-computation:{report_id}"),
            "form_id": _deterministic_uuid(f"report-form:{report_id}"),
            "report_id": report_id,
            "report_version_id": report_version_id,
            "historical_version_id": historical_version_id,
            "supported_lane_id": supported_lane_id,
            "tax_type": tax_domain,
            "tax_year": tax_year,
            "policy_anchor_ids": [],
            "source_anchor_ids": [],
        },
    }


def _orchestration_document_evidence_requirement(
    request: ActionAdapterRequest,
) -> dict[str, object]:
    """Construct the target semantic handoff without exposing extraction keys."""

    requirement_id = build_adapter_request_id(
        request=request, adapter_name="document_evidence_requirement_v1"
    )
    return {
        "requirement_id": requirement_id,
        "schema_version": "1.0.0",
        "semantic_meaning": "information needed from the authorized document",
        "entity_scope": {"selection": "authorized_set"},
        "time_scope": {"kind": "unspecified", "unresolved_requires_confirmation": True},
        "unit": {"dimension": "source_native"},
        "multiplicity": {"kind": "zero_or_more"},
        "completeness": {"coverage": "best_available", "partial_result": "mark_incomplete"},
        "materiality": {"kind": "all_values"},
        "permitted_derivations": ["direct_observation_only"],
        "uncertainty_tolerance": {
            "allowed": [],
            "estimated_values": "prohibited",
            "conflicts": "require_confirmation",
        },
        "confirmation_policy": {
            "mode": "when_triggered",
            "triggers": ["unresolved_time", "conflict"],
        },
        "caller_correlation_reference": request["correlation_id"],
    }


def _build_knowledge_error_response(
    *,
    request: ActionAdapterRequest,
    adapter_name: str,
    reason_code: str,
    message: str,
) -> ActionAdapterResponse:
    trace_id = build_trace_id(request["correlation_id"])
    trace: ActionAdapterTrace = {
        "correlation_id": request["correlation_id"],
        "trace_id": trace_id,
        "adapter_request_id": build_adapter_request_id(
            request=request,
            adapter_name=adapter_name,
        ),
        "adapter_name": adapter_name,
        "submission_payload_ref": request["submission_payload_ref"],
    }
    idempotency_key = request.get("idempotency_key")
    if isinstance(idempotency_key, str):
        trace["idempotency_key"] = idempotency_key
    route_id = request.get("route_id")
    if isinstance(route_id, str):
        trace["route_id"] = route_id
    target_service = request.get("target_service")
    if isinstance(target_service, str):
        trace["target_service"] = target_service
    target_operation = request.get("target_operation")
    if isinstance(target_operation, str):
        trace["target_operation"] = target_operation
    plan_id = request.get("plan_id")
    if isinstance(plan_id, str):
        trace["plan_id"] = plan_id
    step_id = request.get("step_id")
    if isinstance(step_id, str):
        trace["step_id"] = step_id
    capability_context = request["capability_context"]
    return {
        "adapter_status": "unsupported",
        "provider_reference": None,
        "action_result_code": reason_code,
        "message": message,
        "trace": trace,
        "error": {
            "error_code": reason_code,
            "message": message,
            "reason_code": reason_code,
            "reason": reason_code,
            "rejected_context": {
                "action_type": request["action_type"],
                "supported_lane_id": capability_context["supported_lane_id"],
                "historical_version_id": capability_context["historical_version_id"],
                "tax_year": capability_context["tax_year"],
                "correlation_id": request["correlation_id"],
            },
            "required_controls": ["revise_prompt_scope"],
            "next_allowed_actions": ["revise_input", "reject"],
            "trace_id": trace_id,
        },
    }


class DeterministicSubmissionMockActionAdapter:
    """Provide deterministic no-provider submission adapter for pilot abstraction."""

    adapter_name = "deterministic_submission_mock_adapter_v1"
    supported_action_types: tuple[str, ...] = ("submission_execute",)

    def dispatch(self, request: ActionAdapterRequest) -> ActionAdapterResponse:
        return _deterministic_mock_adapter_response(
            request=request,
            adapter_name=self.adapter_name,
            action_result_code="submission_action_mock_pending",
            message=(
                "Submission action accepted by deterministic adapter contract. "
                "External provider execution is not enabled in this phase."
            ),
        )


class DeterministicTaxCoreActionAdapter:
    """Deterministic adapter for tax-core execution route wiring."""

    adapter_name = "deterministic_tax_core_adapter_v1"
    supported_action_types: tuple[str, ...] = ("tax_core_execute_computation",)

    def dispatch(self, request: ActionAdapterRequest) -> ActionAdapterResponse:
        return _deterministic_mock_adapter_response(
            request=request,
            adapter_name=self.adapter_name,
            action_result_code="tax_core_action_mock_pending",
            message="Tax-core route accepted by deterministic adapter registry.",
        )


class DeterministicFormsActionAdapter:
    """Deterministic adapter for forms execution route wiring."""

    adapter_name = "deterministic_forms_adapter_v1"
    supported_action_types: tuple[str, ...] = (
        "forms_generate_income_tax_form_artifact",
        "forms_map_health_contribution_output_to_form_ready",
    )

    def dispatch(self, request: ActionAdapterRequest) -> ActionAdapterResponse:
        if request["action_type"] == "forms_generate_income_tax_form_artifact":
            result_payload = _deterministic_forms_artifact_payload(request)
            provider_reference = cast(str, result_payload["artifact_id"])
            return _deterministic_accepted_adapter_response(
                request=request,
                adapter_name=self.adapter_name,
                action_result_code="forms_artifact_generated",
                message="Forms route generated a governed deterministic artifact payload.",
                provider_reference=provider_reference,
                result_payload=result_payload,
            )

        result_payload = _deterministic_health_form_mapping_payload(request)
        provider_reference = cast(str, result_payload["form_ready_reference"])
        return _deterministic_accepted_adapter_response(
            request=request,
            adapter_name=self.adapter_name,
            action_result_code="forms_mapping_ready",
            message="Forms route produced a governed deterministic form-ready mapping.",
            provider_reference=provider_reference,
            result_payload=result_payload,
        )


class DeterministicReportsActionAdapter:
    """Deterministic adapter for reports execution route wiring."""

    adapter_name = "deterministic_reports_adapter_v1"
    supported_action_types: tuple[str, ...] = (
        "reports_create_income_tax_report_artifact",
        "reports_create_health_contribution_report_artifact",
    )

    def dispatch(self, request: ActionAdapterRequest) -> ActionAdapterResponse:
        tax_domain = (
            "income_tax"
            if request["action_type"] == "reports_create_income_tax_report_artifact"
            else "health_contribution"
        )
        result_payload = _deterministic_report_payload(request, tax_domain=tax_domain)
        provider_reference = cast(str, result_payload["report_id"])
        return _deterministic_accepted_adapter_response(
            request=request,
            adapter_name=self.adapter_name,
            action_result_code="reports_artifact_generated",
            message="Reports route generated a governed deterministic report artifact payload.",
            provider_reference=provider_reference,
            result_payload=result_payload,
        )


class DocumentAIServiceActionAdapter:
    """Call governed Document AI evidence capabilities; never create extraction jobs."""

    adapter_name = "document_ai_service_adapter_v1"
    supported_action_types: tuple[str, ...] = (
        "document_ai_get_document_processing_status",
        "document_ai_search_document_evidence",
        "document_ai_retrieve_document_evidence",
        "document_ai_derive_document_evidence",
        "document_ai_create_workflow_evidence_projection",
    )

    def __init__(self, *, base_url: str | None = None, timeout_seconds: float = 10.0) -> None:
        self._base_url = (base_url or os.getenv("ORCHESTRATION_DOCUMENT_AI_BASE_URL", "")).rstrip(
            "/"
        )
        self._timeout_seconds = timeout_seconds

    def dispatch(self, request: ActionAdapterRequest) -> ActionAdapterResponse:
        document_id = _resolve_document_ai_document_id(request)
        if not self._base_url:
            return _document_ai_rejection(
                request=request,
                adapter_name=self.adapter_name,
                reason_code="document_ai_integration_unconfigured",
                message="Document-AI service URL is not configured; no evidence was requested.",
            )
        if document_id is None:
            return _document_ai_rejection(
                request=request,
                adapter_name=self.adapter_name,
                reason_code="document_ai_document_id_required",
                message="A real uploaded document_id is required before document evidence can be requested.",
            )
        operation = request["action_type"].removeprefix("document_ai_")
        payload = _document_ai_evidence_payload(request=request, document_id=document_id)
        headers = {
            "Content-Type": "application/json",
            "X-Correlation-ID": request["correlation_id"],
            "Idempotency-Key": str(
                request.get("idempotency_key")
                or build_adapter_request_id(request=request, adapter_name=self.adapter_name)
            ),
        }
        auth_context = request.get("auth_context")
        if isinstance(auth_context, dict):
            authorization = auth_context.get("authorization")
            if isinstance(authorization, str) and authorization:
                headers["Authorization"] = authorization
            raw_context = auth_context.get("x_auth_context")
            if isinstance(raw_context, str) and raw_context:
                headers["X-Auth-Context"] = raw_context
        try:
            path, method = _document_ai_operation_transport(operation=operation, document_id=document_id)
            outbound = UrlRequest(
                f"{self._base_url}{path}",
                data=(json.dumps(payload, sort_keys=True).encode("utf-8") if method == "POST" else None),
                headers=headers,
                method=method,
            )
            with urlopen(outbound, timeout=self._timeout_seconds) as response:  # noqa: S310
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError):
            return _document_ai_rejection(
                request=request,
                adapter_name=self.adapter_name,
                reason_code="document_ai_service_request_failed",
                message="Document-AI service could not complete the evidence request.",
            )
        if not isinstance(body, dict):
            return _document_ai_rejection(
                request=request,
                adapter_name=self.adapter_name,
                reason_code="document_ai_service_invalid_response",
                message="Document-AI service returned an invalid evidence response.",
            )
        body = cast(dict[str, object], body)
        result_payload = _normalize_document_ai_evidence_result(
            body=body, document_id=document_id, operation=operation
        )
        lifecycle_status = result_payload.get("lifecycle_status")
        if lifecycle_status in {"uploaded", "processing", "queued", "pending"}:
            action_result_code = "document_evidence_processing_pending"
            message = "Document processing is still pending; evidence limitations were preserved."
        else:
            action_result_code = "document_evidence_resolved"
            message = "Document-AI service returned governed document evidence."
        return _deterministic_accepted_adapter_response(
            request=request,
            adapter_name=self.adapter_name,
            action_result_code=action_result_code,
            message=message,
            provider_reference=str(document_id),
            result_payload=result_payload,
        )


def _document_ai_operation_transport(*, operation: str, document_id: UUID) -> tuple[str, str]:
    paths = {
        "get_document_processing_status": (f"/v1/documents/{document_id}", "GET"),
        "search_document_evidence": ("/v1/document-evidence/hybrid-retrievals", "POST"),
        "retrieve_document_evidence": ("/v1/document-evidence/exact-retrievals", "POST"),
        "derive_document_evidence": ("/v1/document-evidence/derivations", "POST"),
        "create_workflow_evidence_projection": ("/v1/document-evidence/workflow-projections", "POST"),
    }
    return paths[operation]


def _document_ai_evidence_payload(*, request: ActionAdapterRequest, document_id: UUID) -> dict[str, object]:
    route_payload = request.get("route_payload")
    supplied = dict(route_payload) if isinstance(route_payload, dict) else {}
    supplied.pop("document_id", None)
    operation = request["action_type"].removeprefix("document_ai_")
    if operation == "search_document_evidence":
        return {"document_ids": [str(document_id)], "query": str(supplied.get("query") or "document evidence")}
    if operation == "retrieve_document_evidence":
        return {"document_ids": [str(document_id)], "full_text": str(supplied.get("full_text") or supplied.get("query") or "document evidence")}
    return {
        **supplied,
        "document_ids": [str(document_id)],
        "evidence_requirements": [_orchestration_document_evidence_requirement(request)],
    }


def _normalize_document_ai_evidence_result(
    *, body: dict[str, object], document_id: UUID, operation: str
) -> dict[str, object]:
    record = body.get("document")
    record_map = cast(dict[str, object], record) if isinstance(record, dict) else body
    limitations = body.get("evidence_limitations", body.get("limitations", []))
    return {
        **body,
        "document_id": str(document_id),
        "operation": operation,
        "lifecycle_status": record_map.get("state", body.get("status", "ready")),
        "evidence_limitations": limitations if isinstance(limitations, list) else [limitations],
    }


def _resolve_document_ai_document_id(request: ActionAdapterRequest) -> UUID | None:
    route_payload = request.get("route_payload")
    candidate: str | None = None
    if isinstance(route_payload, dict):
        raw_document_id = route_payload.get("document_id")
        if isinstance(raw_document_id, str):
            candidate = raw_document_id
    if not isinstance(candidate, str):
        candidate = request.get("submission_payload_ref")
    try:
        return UUID(str(candidate))
    except (TypeError, ValueError, AttributeError):
        return None


def _document_ai_rejection(
    *, request: ActionAdapterRequest, adapter_name: str, reason_code: str, message: str
) -> ActionAdapterResponse:
    response = _deterministic_mock_adapter_response(
        request=request,
        adapter_name=adapter_name,
        action_result_code=reason_code,
        message=message,
    )
    response["adapter_status"] = "unsupported"
    response["error"] = {
        "error_code": reason_code,
        "message": message,
        "reason_code": reason_code,
        "reason": reason_code,
        "rejected_context": {
            "action_type": request["action_type"],
            "supported_lane_id": request["capability_context"]["supported_lane_id"],
            "historical_version_id": request["capability_context"]["historical_version_id"],
            "tax_year": request["capability_context"]["tax_year"],
            "correlation_id": request["correlation_id"],
        },
        "required_controls": ["upload_document", "provide_document_id"],
        "next_allowed_actions": ["revise_input", "reject"],
        "trace_id": build_trace_id(request["correlation_id"]),
    }
    return response


class DeterministicKnowledgeActionAdapter:
    """Deterministic adapter for governed knowledge search and retrieval route wiring."""

    adapter_name = "deterministic_knowledge_adapter_v1"
    supported_action_types: tuple[str, ...] = (
        "knowledge_search_knowledge",
        "knowledge_retrieve_knowledge",
        "knowledge_timeline_search_knowledge",
    )

    def __init__(self, *, repository: KnowledgeRouteRepository | None) -> None:
        self._repository = repository
        self._search_intelligence = KnowledgeSearchIntelligence()

    def dispatch(self, request: ActionAdapterRequest) -> ActionAdapterResponse:
        route_payload = request.get("route_payload")
        emit_orchestration_debug(
            "KNOWLEDGE",
            "adapter.execution.start",
            action_type=request["action_type"],
            route_id=request.get("route_id"),
            target_operation=request.get("target_operation"),
            tax_domain_hint=_optional_string(route_payload.get("tax_domain"))
            if isinstance(route_payload, dict)
            else None,
        )
        if not isinstance(route_payload, dict):
            emit_orchestration_debug(
                "KNOWLEDGE",
                "adapter.rejected",
                action_type=request["action_type"],
                reason_code="invalid_knowledge_lineage",
                message="Knowledge route payload is missing deterministic lookup context.",
            )
            return _build_knowledge_error_response(
                request=request,
                adapter_name=self.adapter_name,
                reason_code="invalid_knowledge_lineage",
                message="Knowledge route payload is missing deterministic lookup context.",
            )
        emit_orchestration_debug(
            "KNOWLEDGE",
            "adapter.request.normalized",
            action_type=request["action_type"],
            route_id=request.get("route_id"),
            target_operation=request.get("target_operation"),
            query=_optional_string(route_payload.get("query")),
            tax_domain_hint=_optional_string(route_payload.get("tax_domain")),
            resolved_entity=_optional_string(route_payload.get("resolved_entity")),
        )
        fallback_supported = request["action_type"] == "knowledge_search_knowledge"
        emit_orchestration_debug(
            "KNOWLEDGE",
            "web_fallback.eligibility_evaluated",
            trigger="repository_missing" if self._repository is None else "repository_exhausted",
            route_operation=request["action_type"],
            query=_optional_string(route_payload.get("query")),
            tax_domain_hint=_optional_string(route_payload.get("tax_domain")),
            resolved_entity=_optional_string(route_payload.get("resolved_entity")),
            allowed_domains=sorted(EXTRACT_ELIGIBLE_DOMAINS),
            search_depth="advanced",
            web_fallback_eligible=fallback_supported,
        )

        if self._repository is None:
            if fallback_supported:
                return self._dispatch_web_search_fallback(
                    request=request,
                    route_payload=route_payload,
                )
            emit_orchestration_debug(
                "KNOWLEDGE",
                "adapter.rejected",
                action_type=request["action_type"],
                reason_code="unsupported_knowledge_scope",
                message="Knowledge repository is not configured for governed search.",
            )
            return _build_knowledge_error_response(
                request=request,
                adapter_name=self.adapter_name,
                reason_code="unsupported_knowledge_scope",
                message="Knowledge repository is not configured for governed search.",
            )

        try:
            if request["action_type"] == "knowledge_search_knowledge":
                emit_orchestration_debug(
                    "KNOWLEDGE",
                    "adapter.repository.search.requested",
                    action_type=request["action_type"],
                    query=_optional_string(route_payload.get("query")),
                    tax_domain_hint=_optional_string(route_payload.get("tax_domain")),
                    source_type=_optional_string(route_payload.get("source_type")),
                    effective_date=_optional_string(route_payload.get("effective_date")),
                    repository_type=type(self._repository).__name__ if self._repository else None,
                )
                grounded_evidence = self._build_grounded_evidence_from_search_records(
                    records=self._search_records(route_payload=route_payload),
                    knowledge_route_mode="search",
                )
                emit_orchestration_debug(
                    "KNOWLEDGE",
                    "adapter.repository.search.completed",
                    action_type=request["action_type"],
                    record_count=len(grounded_evidence),
                )
                action_result_code = "knowledge_lookup_resolved"
                response_message = (
                    "Knowledge route resolved to governed grounded evidence deterministically."
                )
            elif request["action_type"] == "knowledge_timeline_search_knowledge":
                emit_orchestration_debug(
                    "KNOWLEDGE",
                    "adapter.repository.timeline.requested",
                    action_type=request["action_type"],
                    query=_optional_string(route_payload.get("query")),
                    tax_domain_hint=_optional_string(route_payload.get("tax_domain")),
                    source_type=_optional_string(route_payload.get("source_type")),
                    start_date=_optional_string(route_payload.get("start_date")),
                    end_date=_optional_string(route_payload.get("end_date")),
                    repository_type=type(self._repository).__name__ if self._repository else None,
                )
                grounded_evidence = self._build_grounded_evidence_from_timeline_records(
                    records=self._timeline_search_records(route_payload=route_payload)
                )
                emit_orchestration_debug(
                    "KNOWLEDGE",
                    "adapter.repository.timeline.completed",
                    action_type=request["action_type"],
                    record_count=len(grounded_evidence),
                )
                action_result_code = "knowledge_timeline_resolved"
                response_message = (
                    "Knowledge timeline route resolved to governed "
                    "chronology-safe evidence deterministically."
                )
            else:
                emit_orchestration_debug(
                    "KNOWLEDGE",
                    "adapter.repository.retrieve.requested",
                    action_type=request["action_type"],
                    source_ids=_optional_string(route_payload.get("source_ids")),
                    anchor_ids=_optional_string(route_payload.get("anchor_ids")),
                    repository_type=type(self._repository).__name__ if self._repository else None,
                )
                grounded_evidence = self._build_grounded_evidence_from_search_records(
                    records=self._retrieve_records(route_payload=route_payload),
                    knowledge_route_mode="retrieve",
                )
                emit_orchestration_debug(
                    "KNOWLEDGE",
                    "adapter.repository.retrieve.completed",
                    action_type=request["action_type"],
                    record_count=len(grounded_evidence),
                )
                action_result_code = "knowledge_lookup_resolved"
                response_message = (
                    "Knowledge route resolved to governed grounded evidence deterministically."
                )
        except KnowledgeRepositoryError as error:
            emit_orchestration_debug(
                "KNOWLEDGE",
                "adapter.repository.error",
                action_type=request["action_type"],
                reason_code=error.reason_code,
                message=error.message,
                web_fallback_eligible=fallback_supported,
            )
            if request["action_type"] != "knowledge_retrieve_knowledge":
                return self._dispatch_web_search_fallback(
                    request=request,
                    route_payload=route_payload,
                )
            return _build_knowledge_error_response(
                request=request,
                adapter_name=self.adapter_name,
                reason_code=error.reason_code,
                message=error.message,
            )

        raw_evidence_count = len(grounded_evidence)
        emit_orchestration_debug(
            "KNOWLEDGE",
            "adapter.raw_evidence.built",
            action_type=request["action_type"],
            raw_evidence_count=raw_evidence_count,
        )
        pre_filter_count = len(grounded_evidence)
        grounded_evidence = filter_grounded_evidence_for_scope(
            grounded_evidence,
            tax_domain_hint=_optional_string(route_payload.get("tax_domain")),
            resolved_entity=_optional_string(route_payload.get("resolved_entity")),
            query_text=_optional_string(route_payload.get("query")),
        )
        emit_orchestration_debug(
            "KNOWLEDGE",
            "adapter.scope_filter.result",
            action_type=request["action_type"],
            raw_evidence_count=pre_filter_count,
            filtered_evidence_count=len(grounded_evidence),
        )

        if not grounded_evidence:
            if request["action_type"] == "knowledge_retrieve_knowledge":
                emit_orchestration_debug(
                    "KNOWLEDGE",
                    "adapter.rejected",
                    action_type=request["action_type"],
                    reason_code="invalid_knowledge_identifier",
                    message="No governed published knowledge evidence matched the prompt scope.",
                )
                return _build_knowledge_error_response(
                    request=request,
                    adapter_name=self.adapter_name,
                    reason_code="invalid_knowledge_identifier",
                    message=("No governed published knowledge evidence matched the prompt scope."),
                )
            if fallback_supported:
                return self._dispatch_web_search_fallback(
                    request=request,
                    route_payload=route_payload,
                )
            emit_orchestration_debug(
                "KNOWLEDGE",
                "adapter.rejected",
                action_type=request["action_type"],
                reason_code="domain_evidence_mismatch",
                message=(
                    "Retrieved sources did not contain a passage matching the requested tax "
                    "domain. Please clarify the tax or subject."
                ),
            )
            return _build_knowledge_error_response(
                request=request,
                adapter_name=self.adapter_name,
                reason_code="domain_evidence_mismatch",
                message=(
                    "Retrieved sources did not contain a passage matching the requested tax "
                    "domain. Please clarify the tax or subject."
                ),
            )

        response = _deterministic_mock_adapter_response(
            request=request,
            adapter_name=self.adapter_name,
            action_result_code=action_result_code,
            message=response_message,
        )
        response["adapter_status"] = "accepted"
        response["result_payload"] = {
            "grounding_status": "grounded",
            "grounded_evidence": grounded_evidence,
        }
        emit_orchestration_debug(
            "KNOWLEDGE",
            "adapter.accepted",
            action_type=request["action_type"],
            grounded_evidence_count=len(grounded_evidence),
        )
        return response

    def _dispatch_web_search_fallback(
        self,
        *,
        request: ActionAdapterRequest,
        route_payload: dict[str, object],
    ) -> ActionAdapterResponse:
        query_value = route_payload.get("query")
        if not isinstance(query_value, str) or not query_value.strip():
            emit_orchestration_debug(
                "KNOWLEDGE",
                "web_fallback.rejected",
                route_operation=request["action_type"],
                reason_code="unsupported_knowledge_scope",
                message="Knowledge repository is not configured and no query text is available for web search fallback.",
            )
            return _build_knowledge_error_response(
                request=request,
                adapter_name=self.adapter_name,
                reason_code="unsupported_knowledge_scope",
                message="Knowledge repository is not configured and no query text is available for web search fallback.",  # noqa: E501
            )
        query = query_value.strip()
        tax_year_value = route_payload.get("tax_year")
        tax_year = tax_year_value if isinstance(tax_year_value, int) else None

        tax_domain_hint_value = route_payload.get("tax_domain")
        tax_domain_hint = tax_domain_hint_value if isinstance(tax_domain_hint_value, str) else None
        resolved_entity_value = route_payload.get("resolved_entity")
        resolved_entity = resolved_entity_value if isinstance(resolved_entity_value, str) else None

        client = TavilyWebSearchClient()
        emit_orchestration_debug(
            "KNOWLEDGE",
            "web_fallback.start",
            route_operation=request["action_type"],
            query=query,
            tax_domain_hint=tax_domain_hint,
            resolved_entity=resolved_entity,
            allowed_domains=sorted(EXTRACT_ELIGIBLE_DOMAINS),
            search_depth="advanced",
        )

        # Live KRA extract — run only for time-sensitive domains, before the
        # Tavily search, so the freshest primary source is always prepended.
        kra_extract_evidence: list[dict[str, object]] = []
        if tax_domain_hint in EXTRACT_ELIGIBLE_DOMAINS:
            kra_url = KRA_LIVE_SOURCES.get(tax_domain_hint)
            if kra_url:
                emit_orchestration_debug(
                    "KNOWLEDGE",
                    "web_fallback.kra_extract.requested",
                    route_operation=request["action_type"],
                    query=query,
                    tax_domain_hint=tax_domain_hint,
                    resolved_entity=resolved_entity,
                    kra_url=kra_url,
                )
                try:
                    extracted = client.extract_url(kra_url)
                    if extracted:
                        kra_extract_evidence.append(
                            {
                                "source_id": "web:kra.go.ke",
                                "source_version_id": "web:kra.go.ke",
                                "anchor_id": extracted["url"],
                                "title": f"KRA — {tax_domain_hint.replace('_', ' ').title()} (live)",
                                "url": extracted["url"],
                                "source_type": "web",
                                "authority_level": "primary",
                                "tax_domain": tax_domain_hint,
                                "content_excerpt": extracted["raw_content"],
                                "effective_from": date.today().isoformat(),
                                "effective_to": None,
                                "publication_state": "published",
                                "source_version_form": "web",
                                "grounding_status": "grounded",
                            }
                        )
                        emit_orchestration_debug(
                            "KNOWLEDGE",
                            "web_fallback.kra_extract.completed",
                            route_operation=request["action_type"],
                            tax_domain_hint=tax_domain_hint,
                            raw_web_evidence_count=len(kra_extract_evidence),
                        )
                    else:
                        emit_orchestration_debug(
                            "KNOWLEDGE",
                            "web_fallback.kra_extract.skipped",
                            route_operation=request["action_type"],
                            tax_domain_hint=tax_domain_hint,
                            reason_code="no_extract_content",
                        )
                except Exception as exc:
                    emit_orchestration_debug(
                        "KNOWLEDGE",
                        "web_fallback.kra_extract.failed",
                        route_operation=request["action_type"],
                        tax_domain_hint=tax_domain_hint,
                        exception_type=type(exc).__name__,
                        exception_message=bounded_preview(str(exc), max_length=300),
                    )
                    warnings.warn(
                        f"KRA live extract failed for domain {tax_domain_hint!r} "
                        f"url={kra_url!r}: {exc}",
                        RuntimeWarning,
                        stacklevel=2,
                    )

        try:
            emit_orchestration_debug(
                "KNOWLEDGE",
                "web_fallback.tavily.requested",
                route_operation=request["action_type"],
                query=query,
                tax_domain_hint=tax_domain_hint,
                resolved_entity=resolved_entity,
                allowed_domains=sorted(EXTRACT_ELIGIBLE_DOMAINS),
                search_depth="advanced",
            )
            if resolved_entity is not None:
                results = client.search_tax_topic(
                    query=query,
                    tax_year=tax_year,
                    jurisdiction="Kenya",
                    tax_domain_hint=tax_domain_hint,
                    resolved_entity=resolved_entity,
                )
            else:
                results = client.search_tax_topic(
                    query=query,
                    tax_year=tax_year,
                    jurisdiction="Kenya",
                    tax_domain_hint=tax_domain_hint,
                )
            emit_orchestration_debug(
                "KNOWLEDGE",
                "web_fallback.tavily.completed",
                route_operation=request["action_type"],
                result_count=len(results),
                query=query,
                tax_domain_hint=tax_domain_hint,
                resolved_entity=resolved_entity,
            )
        except TavilySearchError as error:
            emit_orchestration_debug(
                "KNOWLEDGE",
                "web_fallback.tavily.failed",
                route_operation=request["action_type"],
                query=query,
                tax_domain_hint=tax_domain_hint,
                resolved_entity=resolved_entity,
                exception_type=type(error).__name__,
                exception_message=error.message,
                reason_code=error.reason_code,
            )
            return _build_knowledge_error_response(
                request=request,
                adapter_name=self.adapter_name,
                reason_code=error.reason_code,
                message=error.message,
            )

        if not results and not kra_extract_evidence:
            emit_orchestration_debug(
                "KNOWLEDGE",
                "web_fallback.rejected",
                route_operation=request["action_type"],
                query=query,
                tax_domain_hint=tax_domain_hint,
                resolved_entity=resolved_entity,
                reason_code="unsupported_knowledge_scope",
                message=(
                    "No results found in the authorised Kenyan tax sources for this query."
                ),
            )
            return _build_knowledge_error_response(
                request=request,
                adapter_name=self.adapter_name,
                reason_code="unsupported_knowledge_scope",
                message=(
                    "No results found in the authorised Kenyan tax sources "
                    "for this query. Please ask a question related to "
                    "Kenyan tax law."
                ),
            )

        search_evidence: list[dict[str, object]] = [
            {
                "source_id": f"web:{result['domain']}",
                # Web results have no governed version — use source_id as a
                # stable stand-in so GroundedKnowledgeEvidence validation passes.
                "source_version_id": f"web:{result['domain']}",
                "anchor_id": result["source_url"],
                "title": result["title"],
                "url": result["source_url"],
                "source_type": "web",
                "authority_level": result["authority_level"],
                # This is provisional only.  The passage relevance filter
                # below must independently establish the requested subject
                # before this result is eligible to ground an answer.
                "tax_domain": tax_domain_hint or str(route_payload.get("tax_domain", "unknown")),
                "content_excerpt": result["answer_text"] or "",
                # Web results carry no guaranteed effective date — use "unknown"
                # so the temporal disclosure is still renderable.
                "effective_from": result.get("publication_date") or "unknown",
                "effective_to": None,
                # Web pages are live and publicly accessible — treat as published.
                "publication_state": "published",
                # No versioned form for web sources.
                "source_version_form": "web",
                # Carried per-item so GroundedKnowledgeEvidence validation passes.
                "grounding_status": "grounded",
            }
            for result in results
        ]
        # KRA live extract prepended so synthesis sees the freshest primary
        # source first, ahead of ranked Tavily search results.
        grounded_evidence = _filter_evidence_by_freshness(
            [*kra_extract_evidence, *search_evidence],
            tax_domain_hint,
        )
        post_freshness_count = len(grounded_evidence)
        grounded_evidence = filter_grounded_evidence_for_scope(
            grounded_evidence,
            tax_domain_hint=tax_domain_hint,
            resolved_entity=resolved_entity,
            query_text=query,
        )
        emit_orchestration_debug(
            "KNOWLEDGE",
            "web_fallback.filters.completed",
            route_operation=request["action_type"],
            query=query,
            tax_domain_hint=tax_domain_hint,
            resolved_entity=resolved_entity,
            raw_web_evidence_count=len([*kra_extract_evidence, *search_evidence]),
            post_freshness_count=post_freshness_count,
            post_scope_count=len(grounded_evidence),
        )
        if not grounded_evidence:
            emit_orchestration_debug(
                "KNOWLEDGE",
                "web_fallback.rejected",
                route_operation=request["action_type"],
                query=query,
                tax_domain_hint=tax_domain_hint,
                resolved_entity=resolved_entity,
                reason_code="domain_evidence_mismatch",
                message=(
                    "Retrieved sources did not contain a passage matching the requested tax "
                    "domain."
                ),
            )
            return _build_knowledge_error_response(
                request=request,
                adapter_name=self.adapter_name,
                reason_code="domain_evidence_mismatch",
                message=(
                    "Retrieved sources did not contain a passage matching the requested tax "
                    "domain. Please clarify the tax or subject."
                ),
            )

        response = _deterministic_mock_adapter_response(
            request=request,
            adapter_name=self.adapter_name,
            action_result_code="knowledge_lookup_resolved",
            message="Knowledge route resolved via web search fallback (no governed repository).",
        )
        response["adapter_status"] = "accepted"
        response["result_payload"] = {
            "grounding_status": "web_grounded",
            "grounded_evidence": grounded_evidence,
        }
        emit_orchestration_debug(
            "KNOWLEDGE",
            "web_fallback.accepted",
            route_operation=request["action_type"],
            query=query,
            tax_domain_hint=tax_domain_hint,
            resolved_entity=resolved_entity,
            raw_web_evidence_count=len([*kra_extract_evidence, *search_evidence]),
            post_freshness_count=post_freshness_count,
            post_scope_count=len(grounded_evidence),
        )
        return response
    def _search_records(
        self,
        *,
        route_payload: dict[str, object],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        repository = self._repository
        assert repository is not None

        query = route_payload.get("query")
        if not isinstance(query, str) or not query.strip():
            raise KnowledgeRepositoryError(
                reason_code="unsupported_knowledge_scope",
                message="Knowledge search route payload is missing deterministic query text.",
            )

        source_type_value = route_payload.get("source_type")
        source_type = source_type_value if isinstance(source_type_value, str) else None
        tax_domain_value = route_payload.get("tax_domain")
        tax_domain = tax_domain_value if isinstance(tax_domain_value, str) else None
        effective_date_value = route_payload.get("effective_date")
        effective_date = (
            date.fromisoformat(effective_date_value)
            if isinstance(effective_date_value, str) and effective_date_value
            else None
        )
        tax_year_value = route_payload.get("tax_year")
        query_tax_year = tax_year_value if isinstance(tax_year_value, int) else None

        # Phase 1: run original query only and evaluate result quality.
        original_results: list[KnowledgeSearchRecord] = []
        try:
            original_results = list(
                repository.search_records(
                    query=query,
                    source_type=source_type,
                    tax_domain=tax_domain,
                    effective_date=effective_date,
                )
            )
        except KnowledgeRepositoryError:
            pass

        # Phase 2: decide whether expansions are worth trying.
        needs_expansion = True
        if original_results:
            probe_ranked = self._search_intelligence.rank_results(
                results=original_results,
                original_query=query,
                tax_domain=tax_domain or "income_tax",
                query_tax_year=query_tax_year,
            )
            if probe_ranked and probe_ranked[0]["composite_score"] >= EXPANSION_TRIGGER_THRESHOLD:
                needs_expansion = False

        all_results: list[KnowledgeSearchRecord] = list(original_results)

        if needs_expansion:
            expansion_queries = self._search_intelligence.build_search_queries(
                original_query=query,
                tax_domain=tax_domain or "income_tax",
                include_expansions=True,
            )
            for search_query in expansion_queries:
                if search_query["variant_type"] == "original":
                    continue
                try:
                    results = repository.search_records(
                        query=search_query["text"],
                        source_type=source_type,
                        tax_domain=tax_domain,
                        effective_date=effective_date,
                    )
                    all_results.extend(results)
                except KnowledgeRepositoryError:
                    continue

        if not all_results:
            return ()

        # Rank and filter results by relevance, authority, and date-aware currency.
        ranked_results = self._search_intelligence.rank_results(
            results=all_results,
            original_query=query,
            tax_domain=tax_domain or "income_tax",
            query_tax_year=query_tax_year,
        )

        filtered_results = self._search_intelligence.filter_results(
            ranked_results=ranked_results,
            min_confidence=0.5,
            max_results=10,
            query_tax_year=query_tax_year,
        )

        return tuple(result["record"] for result in filtered_results)

    def _retrieve_records(
        self,
        *,
        route_payload: dict[str, object],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        repository = self._repository
        assert repository is not None
        source_ids_value = route_payload.get("source_ids")
        anchor_ids_value = route_payload.get("anchor_ids")
        source_ids = _string_tuple(source_ids_value)
        anchor_ids = _string_tuple(anchor_ids_value)
        if not source_ids and not anchor_ids:
            raise KnowledgeRepositoryError(
                reason_code="invalid_knowledge_identifier",
                message="Knowledge retrieval route payload is missing governed identifiers.",
            )
        return repository.retrieve_records(source_ids=source_ids, anchor_ids=anchor_ids)

    def _timeline_search_records(
        self,
        *,
        route_payload: dict[str, object],
    ) -> tuple[KnowledgeTimelineRecord, ...]:
        repository = self._repository
        assert repository is not None
        query = route_payload.get("query")
        tax_domain = route_payload.get("tax_domain")
        start_date_value = route_payload.get("start_date")
        end_date_value = route_payload.get("end_date")
        if not isinstance(query, str) or not query.strip():
            raise KnowledgeRepositoryError(
                reason_code="unsupported_knowledge_scope",
                message="Knowledge timeline route payload is missing deterministic query text.",
            )
        if not isinstance(tax_domain, str) or not tax_domain.strip():
            raise KnowledgeRepositoryError(
                reason_code="unsupported_knowledge_scope",
                message="Knowledge timeline route payload is missing tax-domain scope.",
            )
        if not isinstance(start_date_value, str) or not isinstance(end_date_value, str):
            raise KnowledgeRepositoryError(
                reason_code="unsupported_knowledge_scope",
                message="Knowledge timeline route payload is missing deterministic date range.",
            )
        try:
            start_date = date.fromisoformat(start_date_value)
            end_date = date.fromisoformat(end_date_value)
        except ValueError as error:
            raise KnowledgeRepositoryError(
                reason_code="invalid_knowledge_request",
                message="Knowledge timeline route payload contains an invalid date range.",
            ) from error
        source_type_value = route_payload.get("source_type")
        source_type = source_type_value if isinstance(source_type_value, str) else None
        return repository.timeline_search_records(
            query=query.strip(),
            source_type=source_type,
            tax_domain=tax_domain.strip(),
            start_date=start_date,
            end_date=end_date,
        )

    def _build_grounded_evidence_from_search_records(
        self,
        *,
        records: tuple[KnowledgeSearchRecord, ...],
        knowledge_route_mode: str,
    ) -> list[dict[str, object]]:
        grounded: list[dict[str, object]] = []
        for record in records:
            source_version = self._resolve_source_version(record=record)
            grounded.append(
                dict(
                    _build_grounded_evidence_item(
                        source_id=record.source_id,
                        source_version_id=source_version.source_version_id,
                        anchor_id=record.anchor_id,
                        title=record.title,
                        url=record.url,
                        source_type=record.source_type,
                        authority_level=record.authority_level,
                        tax_domain=record.tax_domain,
                        effective_from=record.effective_from,
                        effective_to=record.effective_to,
                        tax_year=record.tax_year,
                        content=record.content,
                        publication_state=source_version.publication_state,
                        source_version_form=source_version.source_version_form,
                        canonical_source_ref=record.url,
                        knowledge_route_mode=knowledge_route_mode,
                        timeline_position=None,
                        canonical_claims=(
                            [dict(item) for item in record.canonical_claims]
                            if record.canonical_claims is not None
                            else None
                        ),
                    )
                )
            )
        return grounded

    def _build_grounded_evidence_from_timeline_records(
        self,
        *,
        records: tuple[KnowledgeTimelineRecord, ...],
    ) -> list[dict[str, object]]:
        grounded: list[dict[str, object]] = []
        for record in records:
            if record.publication_state not in SEARCHABLE_KNOWLEDGE_STATES:
                raise KnowledgeRepositoryError(
                    reason_code="invalid_knowledge_lineage",
                    message="Knowledge timeline grounding returned a non-searchable publication state.",
                )
            grounded.append(
                dict(
                    _build_grounded_evidence_item(
                        source_id=record.source_id,
                        source_version_id=record.source_version_id,
                        anchor_id=record.anchor_id,
                        title=record.title,
                        url=record.url,
                        source_type=record.source_type,
                        authority_level=record.authority_level,
                        tax_domain=record.tax_domain,
                        effective_from=record.effective_from,
                        effective_to=record.effective_to,
                        tax_year=None,
                        content=record.content,
                        publication_state=record.publication_state,
                        source_version_form="point_in_time_consolidation",
                        canonical_source_ref=record.url,
                        knowledge_route_mode="timeline_search",
                        timeline_position=record.timeline_position,
                        canonical_claims=None,
                    )
                )
            )
        return grounded

    def _resolve_source_version(
        self,
        *,
        record: KnowledgeSearchRecord,
    ) -> KnowledgeSourceVersionSummaryRecord:
        repository = self._repository
        assert repository is not None
        source_versions = repository.list_source_versions(
            publication_state=None,
            source_id=record.source_id,
            source_family_id=None,
            tax_domain=record.tax_domain,
            source_class=record.source_type,
            limit=100,
            offset=0,
            sort_by="source_family_id",
            sort_order="asc",
        )
        matching: list[KnowledgeSourceVersionSummaryRecord] = []
        for source_version in source_versions:
            if source_version.source_id != record.source_id:
                continue
            if source_version.tax_domain != record.tax_domain:
                continue
            if source_version.source_class != record.source_type:
                continue
            if source_version.publication_state not in SEARCHABLE_KNOWLEDGE_STATES:
                continue
            if source_version.source_input_origin not in OFFICIAL_KNOWLEDGE_ORIGINS:
                continue
            if source_version.effective_from != record.effective_from:
                continue
            if source_version.effective_to != record.effective_to:
                continue
            if source_version.tax_year != record.tax_year:
                continue
            matching.append(source_version)

        if not matching:
            raise KnowledgeRepositoryError(
                reason_code="invalid_knowledge_lineage",
                message="Knowledge grounding could not resolve a published source version.",
            )

        matching.sort(
            key=lambda item: (
                0 if item.publication_state == "published" else 1,
                0 if item.source_version_form == "point_in_time_consolidation" else 1,
                item.source_family_id,
                item.source_version_id,
            )
        )
        return matching[0]


def _tavily_search_client_is_stubbed() -> bool:
    """Return True when tests have replaced the Tavily client with a stub."""

    return getattr(TavilyWebSearchClient, "__module__", "") != "services.orchestration.app.tavily_search_client"


def _repository_supports_web_fallback(repository: KnowledgeRouteRepository | None) -> bool:
    """Return True for lightweight repositories that intentionally rely on web fallback."""

    if repository is None:
        return False
    return not (
        hasattr(repository, "retrieve_records") and hasattr(repository, "list_source_versions")
    )


def _build_grounded_evidence_item(
    *,
    source_id: str,
    source_version_id: str,
    anchor_id: str,
    title: str,
    url: str,
    source_type: str,
    authority_level: str,
    tax_domain: str,
    effective_from: str,
    effective_to: str | None,
    tax_year: int | None,
    publication_state: str,
    source_version_form: str,
    content: str,
    canonical_source_ref: str,
    knowledge_route_mode: str,
    timeline_position: int | None,
    canonical_claims: list[dict[str, object]] | None,
) -> _KnowledgeEvidenceRecord:
    return {
        "source_id": source_id,
        "source_version_id": source_version_id,
        "anchor_id": anchor_id,
        "title": title,
        "url": url,
        "source_type": source_type,
        "authority_level": authority_level,
        "tax_domain": tax_domain,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "tax_year": tax_year,
        "publication_state": publication_state,
        "source_version_form": source_version_form,
        "grounding_status": "grounded",
        "content": content,
        "canonical_source_ref": canonical_source_ref,
        "knowledge_route_mode": knowledge_route_mode,
        "timeline_position": timeline_position,
        "canonical_claims": canonical_claims,
    }


_DEFAULT_SUBMISSION_ADAPTER = DeterministicSubmissionMockActionAdapter()
_ROUTE_ADAPTERS: dict[str, SubmissionActionAdapter] = {
    "tax_core_execute_computation": DeterministicTaxCoreActionAdapter(),
    "forms_generate_income_tax_form_artifact": DeterministicFormsActionAdapter(),
    "forms_map_health_contribution_output_to_form_ready": DeterministicFormsActionAdapter(),
    "reports_create_income_tax_report_artifact": DeterministicReportsActionAdapter(),
    "reports_create_health_contribution_report_artifact": DeterministicReportsActionAdapter(),
    "document_ai_get_document_processing_status": DocumentAIServiceActionAdapter(),
    "document_ai_search_document_evidence": DocumentAIServiceActionAdapter(),
    "document_ai_retrieve_document_evidence": DocumentAIServiceActionAdapter(),
    "document_ai_derive_document_evidence": DocumentAIServiceActionAdapter(),
    "document_ai_create_workflow_evidence_projection": DocumentAIServiceActionAdapter(),
}


def resolve_supported_route_action_type(
    *,
    target_service: str,
    target_operation: str,
) -> str | None:
    """Resolve the canonical adapter action type for one governed route target."""

    return SUPPORTED_ROUTE_ACTIONS.get((target_service, target_operation))


def resolve_submission_action_adapter(
    action_type: str,
) -> SubmissionActionAdapter | None:
    """Resolve deterministic adapter implementation for one action type."""

    if action_type in SUPPORTED_SUBMISSION_ACTION_TYPES:
        return _DEFAULT_SUBMISSION_ADAPTER
    return None


def dispatch_submission_action_request(
    request: ActionAdapterRequest,
) -> ActionAdapterResponse:
    """Dispatch adapter request through registry-resolved deterministic adapter abstraction."""

    adapter = resolve_submission_action_adapter(request["action_type"])
    if adapter is None:
        return build_unsupported_action_response(
            request=request,
            adapter_name="submission_action_adapter_registry",
            reason_code="unsupported_action_type",
            reason="No deterministic adapter is registered for requested action type.",
        )

    return dispatch_submission_action_with_adapter(
        request=request,
        adapter=adapter,
    )


def dispatch_submission_action_request_with_envelope(
    request: ActionExecutionRequest,
) -> ActionExecutionEnvelope:
    """Dispatch request through idempotent execution envelope at adapter boundary."""

    return execute_idempotent_action_request(
        request=request,
        dispatch_adapter_request=dispatch_submission_action_request,
    )


def dispatch_route_action_request(
    request: ActionAdapterRequest,
) -> ActionAdapterResponse:
    """Dispatch adapter request for explicit route-selected downstream service action."""

    target_service = request.get("target_service")
    target_operation = request.get("target_operation")
    if not isinstance(target_service, str) or not isinstance(target_operation, str):
        return build_unsupported_action_response(
            request=request,
            adapter_name="route_action_adapter_registry",
            reason_code="missing_route_target",
            reason="Route execution request is missing target_service or target_operation.",
        )

    mapped_action_type = resolve_supported_route_action_type(
        target_service=target_service,
        target_operation=target_operation,
    )
    if mapped_action_type is None:
        return build_unsupported_action_response(
            request=request,
            adapter_name="route_action_adapter_registry",
            reason_code="unsupported_route_target",
            reason=(
                "Route target service/operation is outside supported deterministic adapter scope."
            ),
        )
    if request["action_type"] != mapped_action_type:
        return build_unsupported_action_response(
            request=request,
            adapter_name="route_action_adapter_registry",
            reason_code="route_action_mismatch",
            reason="Action type does not match deterministic route target mapping.",
        )
    adapter = _ROUTE_ADAPTERS[mapped_action_type]
    return dispatch_submission_action_with_adapter(request=request, adapter=adapter)


def dispatch_route_action_request_with_repository(
    request: ActionAdapterRequest,
    *,
    knowledge_repository: KnowledgeRouteRepository | None = None,
) -> ActionAdapterResponse:
    """Dispatch route request while allowing governed knowledge repository lookup routes."""

    target_service = request.get("target_service")
    target_operation = request.get("target_operation")
    if not isinstance(target_service, str) or not isinstance(target_operation, str):
        return build_unsupported_action_response(
            request=request,
            adapter_name="route_action_adapter_registry",
            reason_code="missing_route_target",
            reason="Route execution request is missing target_service or target_operation.",
        )

    mapped_action_type = resolve_supported_route_action_type(
        target_service=target_service,
        target_operation=target_operation,
    )
    if mapped_action_type is None:
        return build_unsupported_action_response(
            request=request,
            adapter_name="route_action_adapter_registry",
            reason_code="unsupported_route_target",
            reason=(
                "Route target service/operation is outside supported deterministic adapter scope."
            ),
        )
    if request["action_type"] != mapped_action_type:
        return build_unsupported_action_response(
            request=request,
            adapter_name="route_action_adapter_registry",
            reason_code="route_action_mismatch",
            reason="Action type does not match deterministic route target mapping.",
        )
    if mapped_action_type in {
        "knowledge_search_knowledge",
        "knowledge_retrieve_knowledge",
        "knowledge_timeline_search_knowledge",
    }:
        adapter = DeterministicKnowledgeActionAdapter(repository=knowledge_repository)
        return dispatch_submission_action_with_adapter(request=request, adapter=adapter)
    adapter = _ROUTE_ADAPTERS[mapped_action_type]
    return dispatch_submission_action_with_adapter(request=request, adapter=adapter)


def dispatch_route_action_request_with_envelope(
    request: ActionExecutionRequest,
    *,
    knowledge_repository: KnowledgeRouteRepository | None = None,
) -> ActionExecutionEnvelope:
    """Dispatch route-selected request through deterministic idempotent execution envelope."""

    return execute_idempotent_action_request(
        request=request,
        dispatch_adapter_request=lambda adapter_request: (
            dispatch_route_action_request_with_repository(
                adapter_request,
                knowledge_repository=knowledge_repository,
            )
        ),
    )


def dispatch_synthesis_knowledge_tool_request(
    *,
    tool_name: str,
    tool_arguments: dict[str, object],
    correlation_id: str,
    trace_id: str,
    execution_id: str,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
    knowledge_repository: KnowledgeRouteRepository | None,
) -> ActionAdapterResponse:
    """Dispatch one bounded synthesis tool through the governed knowledge adapter."""

    tool_routes: dict[str, tuple[str, str, str]] = {
        "search_records": (
            "knowledge_search_knowledge",
            "knowledge_search_route_v1",
            "search_knowledge",
        ),
        "retrieve_records": (
            "knowledge_retrieve_knowledge",
            "knowledge_retrieve_route_v1",
            "retrieve_knowledge",
        ),
        "timeline_search_records": (
            "knowledge_timeline_search_knowledge",
            "knowledge_timeline_route_v1",
            "timeline_search_knowledge",
        ),
    }
    route = tool_routes.get(tool_name)
    if route is None:
        return build_unsupported_action_response(
            request={
                "action_type": tool_name,
                "correlation_id": correlation_id,
                "submission_payload_ref": execution_id,
                "capability_context": {
                    "supported_lane_id": supported_lane_id,
                    "historical_version_id": historical_version_id,
                    "tax_year": tax_year,
                },
            },
            adapter_name="synthesis_knowledge_tool_adapter",
            reason_code="unsupported_synthesis_tool",
            reason="Synthesis requested a tool outside the governed knowledge allowlist.",
        )

    action_type, route_id, target_operation = route
    route_payload = _build_synthesis_tool_route_payload(
        tool_name=tool_name,
        tool_arguments=tool_arguments,
    )
    return dispatch_route_action_request_with_repository(
        {
            "action_type": action_type,
            "correlation_id": correlation_id,
            "trace_id": trace_id,
            "submission_payload_ref": execution_id,
            "capability_context": {
                "supported_lane_id": supported_lane_id,
                "historical_version_id": historical_version_id,
                "tax_year": tax_year,
            },
            "route_id": route_id,
            "target_service": "knowledge",
            "target_operation": target_operation,
            "route_payload": route_payload,
        },
        knowledge_repository=knowledge_repository,
    )


def _build_synthesis_tool_route_payload(
    *,
    tool_name: str,
    tool_arguments: dict[str, object],
) -> dict[str, object]:
    """Normalize strict tool arguments for the existing knowledge adapter."""

    if tool_name == "retrieve_records":
        return {
            "source_ids": _string_tuple_from_list(tool_arguments.get("source_ids")),
            "anchor_ids": _string_tuple_from_list(tool_arguments.get("anchor_ids")),
        }
    if tool_name == "timeline_search_records":
        return {
            "query": tool_arguments.get("query"),
            "source_type": tool_arguments.get("source_type"),
            "tax_domain": tool_arguments.get("tax_domain"),
            "start_date": tool_arguments.get("start_date"),
            "end_date": tool_arguments.get("end_date"),
        }
    return {
        "query": tool_arguments.get("query"),
        "source_type": tool_arguments.get("source_type"),
        "tax_domain": tool_arguments.get("tax_domain"),
        "effective_date": tool_arguments.get("effective_date"),
    }


def _string_tuple_from_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in cast(list[object], value) if isinstance(item, str) and item)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        return ()
    normalized: list[str] = []
    for item in cast(tuple[object, ...], value):
        if isinstance(item, str) and item:
            normalized.append(item)
    return tuple(normalized)
